"""Frappe persistence adapter and scheduler entrypoint for bulk fan-out."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from .bulk import BulkExecutionPolicy, BulkPlan, BulkSelector, BulkTargetSnapshot
from .bulk_orchestrator import (
    BulkOrchestrationError,
    BulkOrchestrator,
    BulkParentRuntime,
    BulkTargetRuntime,
    InflightCounts,
)
from .frappe_store import FrappeCommandStore
from .feature_flags import runtime_feature_config


_ACTIVE = frozenset({"queued", "leased", "running"})


def _value(row: Any, field: str) -> Any:
    return row.get(field) if isinstance(row, dict) else getattr(row, field)


class FrappeBulkRepository:
    def __init__(self, frappe_module: Any | None = None) -> None:
        if frappe_module is None:
            import frappe as frappe_module
        self.frappe = frappe_module
        self.db = frappe_module.db

    def _rows(self, parent_id: str) -> tuple[Any, ...]:
        rows = self.frappe.get_all(
            "Bulk Operation Target",
            filters={"bulk_operation": parent_id},
            fields=[
                "name", "target_key", "ordinal", "attempt", "server_agent", "bench",
                "managed_site", "agent_id_snapshot", "bench_id_snapshot",
                "site_id_snapshot", "site_domain_snapshot", "environment_snapshot",
                "inventory_revision", "preview_operation", "preview_result_hash",
                "state", "child_operation", "cancellation_operation", "retry_of",
                "retry_source",
            ],
            order_by="ordinal asc, attempt asc",
            limit_page_length=1001,
        )
        latest: dict[int, Any] = {}
        for row in rows:
            ordinal = int(_value(row, "ordinal"))
            previous = latest.get(ordinal)
            if previous is None or int(_value(row, "attempt")) > int(_value(previous, "attempt")):
                latest[ordinal] = row
        return tuple(latest[index] for index in sorted(latest))

    def _runtime_target(self, row: Any) -> BulkTargetRuntime:
        snapshot = BulkTargetSnapshot(
            ordinal=int(_value(row, "ordinal")),
            agent_id=_value(row, "agent_id_snapshot"),
            bench_id=_value(row, "bench_id_snapshot"),
            site_id=_value(row, "site_id_snapshot"),
            site_domain=_value(row, "site_domain_snapshot"),
            environment=_value(row, "environment_snapshot"),
            inventory_revision=_value(row, "inventory_revision"),
        )
        return BulkTargetRuntime(
            snapshot=snapshot,
            state=_value(row, "state"),
            child_operation_id=_value(row, "child_operation") or None,
            attempt=int(_value(row, "attempt")),
            cancellation_sent=bool(_value(row, "cancellation_operation")),
        )

    def _parent_runtime(self, parent_id: str) -> BulkParentRuntime:
        parent = self.db.get_value(
            "Bulk Operation", parent_id,
            [
                "name", "bulk_operation_id", "retry_of", "operation_type", "payload_hash",
                "selector_json", "dry_run_hash", "snapshot_hash", "maximum_targets",
                "canary_size", "global_concurrency", "per_agent_concurrency",
                "per_bench_concurrency", "maximum_failures", "maximum_failure_ratio",
                "state", "pause_requested", "cancel_requested",
            ],
            as_dict=True,
        )
        if not parent:
            raise BulkOrchestrationError("bulk parent does not exist")
        try:
            selector_value = json.loads(parent.selector_json)
            selector = BulkSelector(
                exact_site_ids=tuple(selector_value.get("exact_site_ids", ())),
                environment=selector_value.get("environment"),
                agent_ids=tuple(selector_value.get("agent_ids", ())),
                bench_ids=tuple(selector_value.get("bench_ids", ())),
                labels=tuple(selector_value.get("labels", ())),
            )
            targets = tuple(self._runtime_target(row).snapshot for row in self._rows(parent_id))
            policy = BulkExecutionPolicy(
                maximum_targets=int(parent.maximum_targets),
                canary_size=int(parent.canary_size),
                global_concurrency=int(parent.global_concurrency),
                per_agent_concurrency=int(parent.per_agent_concurrency),
                per_bench_concurrency=int(parent.per_bench_concurrency),
                maximum_failures=int(parent.maximum_failures),
                maximum_failure_ratio=float(parent.maximum_failure_ratio),
            )
            plan = BulkPlan(
                parent_operation_id=parent.bulk_operation_id,
                operation_type=parent.operation_type,
                payload_hash=parent.payload_hash,
                selector=selector,
                targets=targets,
                dry_run_hash=parent.dry_run_hash,
                policy=policy,
                retry_of=parent.retry_of or None,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BulkOrchestrationError("bulk parent contract is corrupt") from exc
        if plan.snapshot_hash != parent.snapshot_hash:
            raise BulkOrchestrationError("bulk parent snapshot hash changed")
        return BulkParentRuntime(
            parent_id=parent_id,
            plan=plan,
            state=parent.state,
            pause_requested=bool(parent.pause_requested),
            cancel_requested=bool(parent.cancel_requested),
        )

    @contextmanager
    def lock_parent(self, parent_id: str) -> Iterator[BulkParentRuntime]:
        rows = self.db.sql(
            "SELECT name FROM `tabBulk Operation` WHERE name=%s FOR UPDATE",
            (parent_id,), as_dict=True,
        )
        if len(rows) != 1:
            raise BulkOrchestrationError("bulk parent does not exist")
        yield self._parent_runtime(parent_id)

    def targets(self, parent_id: str) -> tuple[BulkTargetRuntime, ...]:
        return tuple(self._runtime_target(row) for row in self._rows(parent_id))

    def inflight_counts(self) -> InflightCounts:
        rows = self.db.sql(
            "SELECT a.agent_id, b.bench_id, COUNT(*) AS count "
            "FROM `tabOperation` o JOIN `tabServer Agent` a ON a.name=o.server_agent "
            "JOIN `tabBench` b ON b.name=o.bench "
            "WHERE o.state IN ('queued','leased','running') "
            "GROUP BY a.agent_id, b.bench_id",
            as_dict=True,
        )
        by_agent: dict[str, int] = {}
        by_bench: dict[tuple[str, str], int] = {}
        total = 0
        for row in rows:
            count = int(_value(row, "count"))
            agent = _value(row, "agent_id")
            bench = _value(row, "bench_id")
            total += count
            by_agent[agent] = by_agent.get(agent, 0) + count
            by_bench[(agent, bench)] = count
        return InflightCounts(total, by_agent, by_bench)

    def can_start(self, target: BulkTargetRuntime) -> bool:
        row = self.db.get_value(
            "Server Agent", {"agent_id": target.snapshot.agent_id},
            ["enabled", "drain_requested"], as_dict=True,
        )
        return bool(
            row and row.enabled and not row.drain_requested
            and runtime_feature_config(self.frappe).enabled(
                "bulk_operations", target.snapshot.environment
            )
        )

    def _target_row(self, parent_id: str, target: BulkTargetRuntime) -> Any:
        row = self.db.get_value(
            "Bulk Operation Target",
            {
                "bulk_operation": parent_id,
                "target_key": target.snapshot.target_key,
                "attempt": target.attempt,
            },
            [
                "name", "server_agent", "bench", "managed_site", "preview_operation",
                "preview_result_hash", "retry_of", "child_operation",
                "retry_source", "cancellation_operation", "state",
            ],
            as_dict=True,
        )
        if not row:
            raise BulkOrchestrationError("bulk target disappeared")
        return row

    def create_child(
        self,
        parent_id: str,
        target: BulkTargetRuntime,
        child_operation_id: str,
        idempotency_key: str,
    ) -> None:
        parent = self.db.get_value(
            "Bulk Operation", parent_id,
            [
                "operation_type", "payload_json", "payload_hash", "requested_by",
                "approval_policy", "required_approvals", "approval_count",
                "approval_status", "state",
            ],
            as_dict=True,
        )
        target_row = self._target_row(parent_id, target)
        if (
            not parent or parent.approval_status != "approved"
            or parent.state not in {"approved", "canary", "running"}
            or target_row.state != "planned"
        ):
            raise BulkOrchestrationError("bulk child is not eligible for creation")
        if target_row.child_operation:
            if target_row.child_operation != child_operation_id:
                raise BulkOrchestrationError("bulk target is linked to another child")
            FrappeCommandStore(self.frappe).enqueue_approved_operation(
                child_operation_id, now=datetime.now(UTC)
            )
            return
        previous_child = None
        previous_operation_target = None
        lineage_target = target_row.retry_of or target_row.retry_source
        if lineage_target:
            previous_child = self.db.get_value(
                "Bulk Operation Target", lineage_target, "child_operation"
            )
            previous_operation_target = self.db.get_value(
                "Operation Target", {"operation": previous_child}, "name"
            )
        operation = self.frappe.get_doc({
            "doctype": "Operation",
            "operation_id": child_operation_id,
            "idempotency_key": idempotency_key,
            "protocol_version": "1.0",
            "server_agent": target_row.server_agent,
            "bench": target_row.bench,
            "managed_site": target_row.managed_site,
            "bulk_parent": parent_id,
            "bulk_target": target_row.name,
            "retry_of": previous_child,
            "preview_of": target_row.preview_operation,
            "preview_result_hash": target_row.preview_result_hash,
            "operation_type": parent.operation_type,
            "payload_json": parent.payload_json,
            "payload_hash": parent.payload_hash,
            "requested_by": parent.requested_by,
            "approval_policy": parent.approval_policy,
            "approval_status": "approved",
            "required_approvals": int(parent.required_approvals),
            "approval_count": int(parent.approval_count),
            "state": "awaiting_approval",
        }).insert(ignore_permissions=True)
        identity = json.dumps(
            [
                child_operation_id, target.snapshot.agent_id, target.snapshot.bench_id,
                target_row.managed_site, target.snapshot.inventory_revision,
            ],
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        self.frappe.get_doc({
            "doctype": "Operation Target",
            "operation": operation.name,
            "target_key": hashlib.sha256(identity).hexdigest(),
            "server_agent": target_row.server_agent,
            "bench": target_row.bench,
            "managed_site": target_row.managed_site,
            "site_domain_snapshot": target.snapshot.site_domain,
            "operation_type_snapshot": parent.operation_type,
            "payload_hash_snapshot": parent.payload_hash,
            "inventory_revision_snapshot": target.snapshot.inventory_revision,
            "bulk_parent": parent_id,
            "bulk_target": target_row.name,
            "retry_of": previous_operation_target,
            "state": "awaiting_approval",
            "attempt": target.attempt,
        }).insert(ignore_permissions=True)
        bulk_target = self.frappe.get_doc("Bulk Operation Target", target_row.name)
        bulk_target.child_operation = operation.name
        bulk_target.save(ignore_permissions=True)
        FrappeCommandStore(self.frappe).enqueue_approved_operation(
            operation.name, now=datetime.now(UTC)
        )
        self.db.set_value(
            "Bulk Operation Target", target_row.name, "state", "queued",
            update_modified=False,
        )

    def request_child_cancel(self, parent_id: str, target: BulkTargetRuntime) -> None:
        parent = self.db.get_value(
            "Bulk Operation", parent_id,
            ["cancel_requested", "cancel_requested_by"], as_dict=True,
        )
        row = self._target_row(parent_id, target)
        if not parent or not parent.cancel_requested or not parent.cancel_requested_by:
            raise BulkOrchestrationError("bulk cancellation is not attributed")
        if not row.child_operation or row.state in {
            "succeeded", "failed", "cancelled", "timed_out", "dead_letter",
            "needs_intervention", "rejected",
        }:
            raise BulkOrchestrationError("bulk target is not cancellable")
        namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"frappe-controller:bulk-cancel:{parent_id}")
        cancellation_id = str(uuid.uuid5(namespace, row.child_operation))
        if row.cancellation_operation:
            if row.cancellation_operation != cancellation_id:
                raise BulkOrchestrationError("bulk cancellation identity changed")
            return
        payload = {"target_operation_id": row.child_operation}
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        current_revision = self.db.get_value(
            "Server Agent", row.server_agent, "inventory_digest"
        )
        if not isinstance(current_revision, str) or not re.fullmatch(
            r"[0-9a-f]{64}", current_revision
        ):
            raise BulkOrchestrationError("bulk cancellation target inventory is unavailable")
        operation = self.frappe.get_doc({
            "doctype": "Operation",
            "operation_id": cancellation_id,
            "idempotency_key": f"controller:bulk-cancel:{cancellation_id}",
            "protocol_version": "1.0",
            "server_agent": row.server_agent,
            "bench": row.bench,
            "managed_site": row.managed_site,
            "operation_type": "operation.cancel",
            "payload_json": payload_json,
            "payload_hash": payload_hash,
            "requested_by": parent.cancel_requested_by,
            "approval_status": "not_required",
            "required_approvals": 0,
            "approval_count": 0,
            "state": "awaiting_approval",
        }).insert(ignore_permissions=True)
        identity = json.dumps(
            [cancellation_id, target.snapshot.agent_id, target.snapshot.bench_id,
             row.managed_site, current_revision],
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        self.frappe.get_doc({
            "doctype": "Operation Target",
            "operation": operation.name,
            "target_key": hashlib.sha256(identity).hexdigest(),
            "server_agent": row.server_agent,
            "bench": row.bench,
            "managed_site": row.managed_site,
            "site_domain_snapshot": target.snapshot.site_domain,
            "operation_type_snapshot": "operation.cancel",
            "payload_hash_snapshot": payload_hash,
            "inventory_revision_snapshot": current_revision,
            "state": "awaiting_approval",
            "attempt": 0,
        }).insert(ignore_permissions=True)
        bulk_target = self.frappe.get_doc("Bulk Operation Target", row.name)
        bulk_target.cancellation_operation = operation.name
        bulk_target.save(ignore_permissions=True)
        FrappeCommandStore(self.frappe).enqueue_approved_operation(
            operation.name, now=datetime.now(UTC)
        )

    def mark_unstarted_cancelled(self, parent_id: str, target: BulkTargetRuntime) -> None:
        row = self._target_row(parent_id, target)
        if row.child_operation or row.state != "planned":
            raise BulkOrchestrationError("only an unstarted bulk target can be cancelled locally")
        self.db.set_value(
            "Bulk Operation Target", row.name, "state", "cancelled",
            update_modified=False,
        )

    def update_parent(self, parent_id: str, state: str, aggregate: Any) -> None:
        parent = self.frappe.get_doc("Bulk Operation", parent_id)
        parent.state = state
        parent.planned_count = aggregate.planned
        parent.queued_count = aggregate.queued
        parent.running_count = aggregate.running
        parent.succeeded_count = aggregate.succeeded
        parent.failed_count = aggregate.failed
        parent.cancelled_count = aggregate.cancelled
        parent.needs_intervention_count = aggregate.needs_intervention
        if state in {"canary", "running"} and not parent.started_at:
            parent.started_at = datetime.now(UTC)
        if state in {"succeeded", "partial", "failed", "cancelled", "needs_intervention"}:
            parent.completed_at = datetime.now(UTC)
        parent.save(ignore_permissions=True)

def reconcile_bulk_operations() -> None:
    import frappe

    names = frappe.get_all(
        "Bulk Operation",
        filters={"state": ["in", ["approved", "canary", "running", "paused", "cancelling"]]},
        pluck="name",
        order_by="creation asc",
        limit_page_length=100,
    )
    repository = FrappeBulkRepository(frappe)
    orchestrator = BulkOrchestrator(repository)
    for name in names:
        savepoint = f"bulk_{str(name).replace('-', '')[:32]}"
        frappe.db.savepoint(savepoint)
        try:
            orchestrator.reconcile(name)
        except BulkOrchestrationError:
            frappe.db.rollback(save_point=savepoint)
            frappe.db.set_value(
                "Bulk Operation", name,
                {"state": "needs_intervention", "completed_at": datetime.now(UTC)},
                update_modified=False,
            )
            frappe.log_error(
                title="Bulk orchestration contract failure",
                message=f"Bulk Operation {name} failed closed during reconciliation",
            )


__all__ = ["FrappeBulkRepository", "reconcile_bulk_operations"]
