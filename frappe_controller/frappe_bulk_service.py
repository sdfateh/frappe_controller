"""Transactional authoring of preview-bound bulk data-update parents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from frappe.utils import now_datetime

from .api.data_updates import validate_preview_result
from .bulk import BulkExecutionPolicy, BulkPlan, BulkSelector
from .bulk_selection import BulkSelectionService
from .data_update_authoring import canonical_hash, normalize_data_update_command
from .frappe_bulk_selection import FrappeBulkSelectionRepository
from .frappe_operation_service import FrappeOperationAuthoringRepository
from .operation_service import OperationAuthoringError, select_approval_rule


_OPERATIONS = frozenset({"data.update", "data.update.break_glass"})


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _row_value(row: Any, field: str) -> Any:
    return row.get(field) if isinstance(row, Mapping) else getattr(row, field)


def _policy(frappe: Any, operation: str, environment: str, count: int) -> Any:
    repository = FrappeOperationAuthoringRepository(frappe)
    rule = select_approval_rule(repository.approval_rules(), environment, operation)
    if rule is None:
        raise OperationAuthoringError("bulk operation requires an approval policy")
    policy = frappe.db.get_value(
        "Approval Policy", rule.name,
        [
            "name", "enabled", "minimum_approvals", "require_backup",
            "bulk_threshold", "maximum_targets",
        ],
        as_dict=True,
    )
    if not policy or not policy.enabled:
        raise OperationAuthoringError("bulk approval policy is disabled")
    maximum = int(policy.maximum_targets)
    threshold = int(policy.bulk_threshold)
    if not 1 <= maximum <= 1000 or not 1 <= threshold <= maximum:
        raise OperationAuthoringError("bulk approval policy bounds are invalid")
    if count < threshold:
        raise OperationAuthoringError("selection is below the approval policy bulk threshold")
    if count > maximum:
        raise OperationAuthoringError("bulk target count exceeds approval policy")
    # Data updates do not claim that a separate backup was taken. A policy that
    # requires one therefore cannot authorize this operation family.
    if bool(policy.require_backup):
        raise OperationAuthoringError("bulk approval policy requires an unavailable backup")
    return policy


def _preview_rows(frappe: Any, ids: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    operations = frappe.get_all(
        "Operation",
        filters={"name": ["in", list(ids)]},
        fields=[
            "name", "operation_id", "operation_type", "server_agent", "bench",
            "managed_site", "requested_by", "state", "payload_json", "payload_hash",
            "result_json", "result_hash",
        ],
        order_by="name asc",
        limit_page_length=1001,
    )
    targets = frappe.get_all(
        "Operation Target",
        filters={"operation": ["in", list(ids)]},
        fields=["operation", "inventory_revision_snapshot"],
        order_by="operation asc",
        limit_page_length=1001,
    )
    return (
        {_row_value(row, "name"): row for row in operations},
        {_row_value(row, "operation"): row for row in targets},
    )


def _identical_existing(
    frappe: Any,
    *,
    operation_id: str,
    expected_parent: Mapping[str, Any],
    expected_targets: Sequence[Mapping[str, Any]],
) -> Any | None:
    if not frappe.db.exists("Bulk Operation", operation_id):
        return None
    fields = (
        "bulk_operation_id", "retry_of", "idempotency_key", "operation_type", "payload_json",
        "payload_hash", "selector_json", "selector_hash", "snapshot_hash",
        "dry_run_hash", "environment_counts_json", "requested_by", "approval_policy",
        "required_approvals", "maximum_targets", "canary_size", "global_concurrency",
        "per_agent_concurrency", "per_bench_concurrency", "maximum_failures",
        "maximum_failure_ratio", "total_targets",
    )
    row = frappe.db.get_value("Bulk Operation", operation_id, list(fields), as_dict=True)
    if not row or any(_row_value(row, field) != expected_parent[field] for field in fields):
        raise OperationAuthoringError("bulk operation id is already bound to different data")
    rows = frappe.get_all(
        "Bulk Operation Target",
        filters={"bulk_operation": operation_id, "attempt": 0},
        fields=[
            "target_key", "preview_operation", "preview_result_hash",
            "inventory_revision", "retry_source",
        ],
        order_by="ordinal asc",
        limit_page_length=1001,
    )
    actual = [
        {
            "target_key": _row_value(item, "target_key"),
            "preview_operation": _row_value(item, "preview_operation"),
            "preview_result_hash": _row_value(item, "preview_result_hash"),
            "inventory_revision": _row_value(item, "inventory_revision"),
            "retry_source": _row_value(item, "retry_source"),
        }
        for item in rows
    ]
    expected = [
        {field: item[field] for field in (
            "target_key", "preview_operation", "preview_result_hash", "inventory_revision",
            "retry_source",
        )}
        for item in expected_targets
    ]
    if actual != expected:
        raise OperationAuthoringError("bulk operation targets differ from existing data")
    return frappe.get_doc("Bulk Operation", operation_id)


def create_preview_bound_bulk_data_update(
    frappe: Any,
    *,
    operation_id: str,
    operation_type: str,
    selector: BulkSelector,
    preview_operation_ids: Sequence[str],
    actor: str,
    retry_of: str | None = None,
    retry_sources: Mapping[str, str] | None = None,
) -> Any:
    """Create one immutable parent and target set in the current DB transaction."""
    if operation_type not in _OPERATIONS:
        raise OperationAuthoringError("unsupported bulk operation type")
    retry_sources = dict(retry_sources or {})
    if bool(retry_of) != bool(retry_sources):
        raise OperationAuthoringError("bulk retry lineage is incomplete")
    selection = BulkSelectionService(
        FrappeBulkSelectionRepository(frappe), system_maximum=1000
    ).resolve(
        selector, operation_type=operation_type, policy_maximum=1000,
        allow_mixed_environments=False,
    )
    environments = {target.environment for target in selection.targets}
    if len(environments) != 1:
        raise OperationAuthoringError("bulk selection must resolve one environment")
    policy_row = _policy(frappe, operation_type, next(iter(environments)), len(selection.targets))
    maximum_targets = int(policy_row.maximum_targets)
    if len(selection.targets) > maximum_targets:
        raise OperationAuthoringError("bulk target count exceeds approval policy")
    if len(preview_operation_ids) != len(selection.targets):
        raise OperationAuthoringError("one successful preview is required per target")
    target_keys = {target.target_key for target in selection.targets}
    if retry_of and (
        set(retry_sources) != target_keys
        or any(not isinstance(value, str) or not value for value in retry_sources.values())
    ):
        raise OperationAuthoringError("bulk retry sources do not match the resolved targets")

    previews, preview_targets = _preview_rows(frappe, preview_operation_ids)
    if set(previews) != set(preview_operation_ids) or set(preview_targets) != set(preview_operation_ids):
        raise OperationAuthoringError("one or more preview operations are missing")
    previews_by_target: dict[tuple[str, str, str], Any] = {}
    for preview_id in preview_operation_ids:
        preview = previews[preview_id]
        key = tuple(_row_value(preview, field) for field in (
            "server_agent", "bench", "managed_site"
        ))
        if key in previews_by_target:
            raise OperationAuthoringError("duplicate preview target")
        previews_by_target[key] = preview

    common_apply: dict[str, Any] | None = None
    target_values: list[dict[str, Any]] = []
    dry_run_evidence: list[dict[str, str]] = []
    for snapshot, site in zip(selection.targets, selection.selected_sites, strict=True):
        key = (site.server_agent, site.bench, site.managed_site)
        preview = previews_by_target.get(key)
        if preview is None:
            raise OperationAuthoringError("preview set does not match the resolved targets")
        preview_id = _row_value(preview, "operation_id")
        result_json = _row_value(preview, "result_json")
        result_hash = _row_value(preview, "result_hash")
        target_row = preview_targets[preview_id]
        if (
            _row_value(preview, "requested_by") != actor
            or _row_value(preview, "operation_type") != operation_type
            or _row_value(preview, "state") != "succeeded"
            or not isinstance(result_json, str)
            or not isinstance(result_hash, str)
            or hashlib.sha256(result_json.encode("utf-8")).hexdigest() != result_hash
            or _row_value(target_row, "inventory_revision_snapshot") != snapshot.inventory_revision
        ):
            raise OperationAuthoringError("preview identity, state, or inventory revision is invalid")
        try:
            preview_command = normalize_data_update_command(
                operation_type, json.loads(_row_value(preview, "payload_json")), dry_run=True
            )
        except (TypeError, json.JSONDecodeError):
            raise OperationAuthoringError("preview payload is invalid") from None
        if canonical_hash(preview_command) != _row_value(preview, "payload_hash"):
            raise OperationAuthoringError("preview payload hash is invalid")
        validate_preview_result(
            result_json,
            preview={
                "operation_id": preview_id,
                "operation_type": operation_type,
                "requested_by": actor,
            },
            command=preview_command,
            target={
                "agent_id": snapshot.agent_id,
                "bench_id": snapshot.bench_id,
                "site_domain": snapshot.site_domain,
            },
        )
        apply_command = {
            **preview_command,
            "payload": {**preview_command["payload"], "dry_run": False},
        }
        if common_apply is None:
            common_apply = apply_command
        elif common_apply != apply_command:
            raise OperationAuthoringError("bulk previews do not authorize one common payload")
        dry_run_evidence.append({
            "target_key": snapshot.target_key,
            "preview_operation": preview_id,
            "preview_result_hash": result_hash,
        })
        target_values.append({
            "bulk_operation": operation_id,
            "target_key": snapshot.target_key,
            "ordinal": snapshot.ordinal,
            "wave": 0 if snapshot.ordinal == 0 else 1,
            "attempt": 0,
            "server_agent": site.server_agent,
            "bench": site.bench,
            "managed_site": site.managed_site,
            "agent_id_snapshot": snapshot.agent_id,
            "bench_id_snapshot": snapshot.bench_id,
            "site_id_snapshot": snapshot.site_id,
            "site_domain_snapshot": snapshot.site_domain,
            "environment_snapshot": snapshot.environment,
            "inventory_revision": snapshot.inventory_revision,
            "preview_operation": preview_id,
            "preview_result_hash": result_hash,
            "retry_source": retry_sources.get(snapshot.target_key),
            "state": "planned",
        })

    if common_apply is None:
        raise OperationAuthoringError("bulk operation has no payload")
    payload_json = _canonical(common_apply)
    payload_hash = canonical_hash(common_apply)
    selector_value = asdict(selector)
    selector_json = _canonical(selector_value)
    selector_hash = hashlib.sha256(selector_json.encode("utf-8")).hexdigest()
    dry_run_hash = hashlib.sha256(_canonical(dry_run_evidence).encode("utf-8")).hexdigest()
    execution_policy = BulkExecutionPolicy(
        maximum_targets=maximum_targets,
        canary_size=1,
        global_concurrency=min(10, len(selection.targets)),
        per_agent_concurrency=min(2, len(selection.targets)),
        per_bench_concurrency=1,
        maximum_failures=0,
        maximum_failure_ratio=0.0,
    )
    plan = BulkPlan(
        parent_operation_id=operation_id,
        operation_type=operation_type,
        payload_hash=payload_hash,
        selector=selector,
        targets=selection.targets,
        dry_run_hash=dry_run_hash,
        policy=execution_policy,
        retry_of=retry_of,
    )
    timestamp = now_datetime()
    environment_counts_json = _canonical(dict(selection.environment_counts))
    parent_values = {
        "bulk_operation_id": operation_id,
        "retry_of": retry_of,
        "idempotency_key": f"controller:bulk:{operation_id}",
        "operation_type": operation_type,
        "payload_json": payload_json,
        "payload_hash": payload_hash,
        "selector_json": selector_json,
        "selector_hash": selector_hash,
        "snapshot_hash": plan.snapshot_hash,
        "dry_run_hash": dry_run_hash,
        "environment_counts_json": environment_counts_json,
        "requested_by": actor,
        "approval_policy": _row_value(policy_row, "name"),
        "approval_status": "pending",
        "required_approvals": int(policy_row.minimum_approvals),
        "maximum_targets": maximum_targets,
        "canary_size": execution_policy.canary_size,
        "global_concurrency": execution_policy.global_concurrency,
        "per_agent_concurrency": execution_policy.per_agent_concurrency,
        "per_bench_concurrency": execution_policy.per_bench_concurrency,
        "maximum_failures": execution_policy.maximum_failures,
        "maximum_failure_ratio": execution_policy.maximum_failure_ratio,
        "state": "awaiting_approval",
        "total_targets": len(selection.targets),
        "planned_count": len(selection.targets),
        "queued_count": 0,
        "running_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "cancelled_count": 0,
        "needs_intervention_count": 0,
        "pause_requested": 0,
        "cancel_requested": 0,
        "requested_at": timestamp,
        "previewed_at": timestamp,
    }
    existing = _identical_existing(
        frappe, operation_id=operation_id, expected_parent=parent_values,
        expected_targets=target_values,
    )
    if existing is not None:
        return existing

    parent = frappe.get_doc({"doctype": "Bulk Operation", **parent_values})
    parent.flags.controller_service = True
    parent.insert(ignore_permissions=True)
    for values in target_values:
        target = frappe.get_doc({"doctype": "Bulk Operation Target", **values})
        target.flags.controller_service = True
        target.insert(ignore_permissions=True)
    return parent


def decide_bulk_operation(
    frappe: Any, *, operation_id: str, decision: str, comment: str
) -> Any:
    if decision not in {"approved", "rejected"}:
        raise OperationAuthoringError("bulk approval decision is invalid")
    if not isinstance(comment, str) or len(comment.encode("utf-8")) > 2000 or "\x00" in comment:
        raise OperationAuthoringError("bulk approval comment is invalid")
    policy = frappe.db.get_value("Bulk Operation", operation_id, "approval_policy")
    if not policy:
        raise OperationAuthoringError("bulk operation does not exist")
    approval = frappe.get_doc({
        "doctype": "Bulk Operation Approval",
        "bulk_operation": operation_id,
        "approval_policy": policy,
        "decision": decision,
        "comment": comment,
    })
    approval.flags.controller_service = True
    return approval.insert(ignore_permissions=True)


__all__ = ["create_preview_bound_bulk_data_update", "decide_bulk_operation"]
