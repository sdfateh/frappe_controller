"""Authenticated fixed APIs for preview-bound bulk data updates."""

from __future__ import annotations

import json
import uuid
from typing import Any, Mapping

import frappe
from frappe.utils import now_datetime

from ..bulk import BulkContractError, BulkSelector
from ..frappe_bulk_service import (
    create_preview_bound_bulk_data_update,
    decide_bulk_operation as persist_bulk_decision,
)
from ..feature_flags import (
    bulk_parent_enabled,
    require_feature,
    target_environment,
)
from ..operation_service import OperationAuthoringError
from .operations import _pairs, _require_author


_CREATE_FIELDS = frozenset({
    "bulk_operation_id", "operation_type", "selector", "preview_operation_ids",
})
_RETRY_FIELDS = frozenset({
    "bulk_operation_id", "source_bulk_operation_id", "preview_operation_ids",
})
_SELECTOR_FIELDS = frozenset({
    "exact_site_ids", "environment", "agent_ids", "bench_ids", "labels",
})
_READ_ROLES = frozenset({"Controller Admin", "Operator", "Approver", "Auditor"})


def _require_bulk_previews(preview_ids: tuple[str, ...]) -> None:
    rows = frappe.get_all(
        "Operation",
        filters={"name": ["in", list(preview_ids)]},
        fields=["name", "server_agent", "managed_site"],
        limit_page_length=1001,
    )
    if len(rows) != len(preview_ids):
        frappe.throw("Controller feature is disabled", frappe.PermissionError)
    for row in rows:
        require_feature(
            frappe,
            "bulk_operations",
            target_environment(frappe, row.server_agent, row.managed_site),
        )


def _require_bulk_parent(operation_id: str) -> None:
    if not bulk_parent_enabled(frappe, operation_id):
        frappe.throw("Controller feature is disabled", frappe.PermissionError)


def _uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise OperationAuthoringError(f"{field} must be a UUID")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise OperationAuthoringError(f"{field} must be a UUID") from None
    if canonical != value.lower():
        raise OperationAuthoringError(f"{field} must be canonical")
    return canonical


def _endpoint_uuid(value: Any, field: str) -> str:
    try:
        return _uuid(value, field)
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)


def _create_request(request_json: str) -> tuple[str, str, BulkSelector, tuple[str, ...]]:
    if not isinstance(request_json, str) or len(request_json.encode("utf-8")) > 256 * 1024:
        raise OperationAuthoringError("bulk request is invalid")
    try:
        value = json.loads(
            request_json, object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        raise OperationAuthoringError("bulk request is invalid JSON") from None
    if not isinstance(value, Mapping) or set(value) != _CREATE_FIELDS:
        raise OperationAuthoringError("bulk request fields do not match the contract")
    selector_value = value["selector"]
    if not isinstance(selector_value, Mapping) or set(selector_value) != _SELECTOR_FIELDS:
        raise OperationAuthoringError("bulk selector fields do not match the contract")
    preview_ids = value["preview_operation_ids"]
    if not isinstance(preview_ids, list) or not preview_ids or len(preview_ids) > 1000:
        raise OperationAuthoringError("bulk preview list is invalid")
    normalized_previews = tuple(_uuid(item, "preview operation id") for item in preview_ids)
    if len(set(normalized_previews)) != len(normalized_previews):
        raise OperationAuthoringError("bulk preview list contains duplicates")
    operation_type = value["operation_type"]
    if operation_type not in {"data.update", "data.update.break_glass"}:
        raise OperationAuthoringError("unsupported bulk operation type")
    sequence_fields = ("exact_site_ids", "agent_ids", "bench_ids", "labels")
    if any(not isinstance(selector_value[field], list) for field in sequence_fields):
        raise OperationAuthoringError("bulk selector is invalid")
    if selector_value["environment"] is not None and not isinstance(
        selector_value["environment"], str
    ):
        raise OperationAuthoringError("bulk selector is invalid")
    try:
        selector = BulkSelector(
            exact_site_ids=tuple(selector_value["exact_site_ids"]),
            environment=selector_value["environment"],
            agent_ids=tuple(selector_value["agent_ids"]),
            bench_ids=tuple(selector_value["bench_ids"]),
            labels=tuple(selector_value["labels"]),
        )
    except (TypeError, BulkContractError):
        raise OperationAuthoringError("bulk selector is invalid") from None
    return (
        _uuid(value["bulk_operation_id"], "bulk operation id"),
        operation_type,
        selector,
        normalized_previews,
    )


@frappe.whitelist(methods=["POST"])
def create_bulk_data_update(request_json: str) -> Mapping[str, Any]:
    actor = _require_author()
    try:
        operation_id, operation_type, selector, previews = _create_request(request_json)
        _require_bulk_previews(previews)
        parent = create_preview_bound_bulk_data_update(
            frappe,
            operation_id=operation_id,
            operation_type=operation_type,
            selector=selector,
            preview_operation_ids=previews,
            actor=actor,
        )
        return {
            "bulk_operation_id": parent.bulk_operation_id,
            "state": parent.state,
            "approval_status": parent.approval_status,
            "required_approvals": int(parent.required_approvals),
            "total_targets": int(parent.total_targets),
            "payload_hash": parent.payload_hash,
            "snapshot_hash": parent.snapshot_hash,
            "dry_run_hash": parent.dry_run_hash,
        }
    except (OperationAuthoringError, BulkContractError) as exc:
        frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def retry_failed_bulk_data_update(request_json: str) -> Mapping[str, Any]:
    actor = _require_author()
    try:
        if not isinstance(request_json, str) or len(request_json.encode("utf-8")) > 128 * 1024:
            raise OperationAuthoringError("bulk retry request is invalid")
        try:
            value = json.loads(
                request_json, object_pairs_hook=_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            raise OperationAuthoringError("bulk retry request is invalid JSON") from None
        if not isinstance(value, Mapping) or set(value) != _RETRY_FIELDS:
            raise OperationAuthoringError("bulk retry request fields do not match the contract")
        operation_id = _uuid(value["bulk_operation_id"], "bulk operation id")
        source_id = _uuid(value["source_bulk_operation_id"], "source bulk operation id")
        preview_ids = value["preview_operation_ids"]
        if not isinstance(preview_ids, list) or not preview_ids or len(preview_ids) > 1000:
            raise OperationAuthoringError("bulk retry preview list is invalid")
        previews = tuple(_uuid(item, "preview operation id") for item in preview_ids)
        if len(set(previews)) != len(previews):
            raise OperationAuthoringError("bulk retry preview list contains duplicates")
        _require_bulk_previews(previews)
        source = frappe.db.get_value(
            "Bulk Operation", source_id,
            ["operation_type", "requested_by", "state"], as_dict=True,
        )
        if not source:
            raise OperationAuthoringError("source bulk operation does not exist")
        if source.requested_by != actor and "Controller Admin" not in frappe.get_roles(actor):
            frappe.throw(
                "only the requester or Controller Admin may retry this bulk operation",
                frappe.PermissionError,
            )
        if source.state not in {"partial", "failed", "paused", "needs_intervention"}:
            raise OperationAuthoringError("source bulk operation is not retryable")
        rows = frappe.get_all(
            "Bulk Operation Target",
            filters={"bulk_operation": source_id},
            fields=["name", "target_key", "site_id_snapshot", "attempt", "state"],
            order_by="ordinal asc, attempt desc",
            limit_page_length=1001,
        )
        latest: dict[str, Any] = {}
        for row in rows:
            if row.target_key not in latest:
                latest[row.target_key] = row
        failure_states = {
            "failed", "timed_out", "dead_letter", "needs_intervention", "rejected",
        }
        failed = tuple(row for row in latest.values() if row.state in failure_states)
        if not failed or len(failed) != len(previews):
            raise OperationAuthoringError("one fresh preview is required per failed target")
        source_names = [row.name for row in failed]
        existing_retries = frappe.get_all(
            "Bulk Operation Target",
            filters={"retry_source": ["in", source_names]},
            fields=["bulk_operation", "retry_source"],
            limit_page_length=1001,
        )
        if existing_retries and (
            {row.retry_source for row in existing_retries} != set(source_names)
            or any(row.bulk_operation != operation_id for row in existing_retries)
        ):
            raise OperationAuthoringError("one or more failed targets already have a retry lineage")
        selector = BulkSelector(exact_site_ids=tuple(row.site_id_snapshot for row in failed))
        retry_sources = {row.target_key: row.name for row in failed}
        parent = create_preview_bound_bulk_data_update(
            frappe,
            operation_id=operation_id,
            operation_type=source.operation_type,
            selector=selector,
            preview_operation_ids=previews,
            actor=actor,
            retry_of=source_id,
            retry_sources=retry_sources,
        )
        return {
            "bulk_operation_id": parent.bulk_operation_id,
            "retry_of": parent.retry_of,
            "state": parent.state,
            "approval_status": parent.approval_status,
            "required_approvals": int(parent.required_approvals),
            "total_targets": int(parent.total_targets),
            "snapshot_hash": parent.snapshot_hash,
            "dry_run_hash": parent.dry_run_hash,
        }
    except (OperationAuthoringError, BulkContractError) as exc:
        frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def decide_bulk_operation(
    bulk_operation_id: str, decision: str, comment: str = ""
) -> Mapping[str, Any]:
    actor = frappe.session.user
    if not actor or actor == "Guest" or "Approver" not in frappe.get_roles(actor):
        frappe.throw("Approver role is required", frappe.PermissionError)
    try:
        operation_id = _uuid(bulk_operation_id, "bulk operation id")
        _require_bulk_parent(operation_id)
        approval = persist_bulk_decision(
            frappe, operation_id=operation_id, decision=decision, comment=comment
        )
        parent = frappe.db.get_value(
            "Bulk Operation", operation_id,
            ["state", "approval_status", "approval_count", "required_approvals"],
            as_dict=True,
        )
        return {
            "bulk_operation_id": operation_id,
            "approval_id": approval.name,
            "decision": approval.decision,
            "state": parent.state,
            "approval_status": parent.approval_status,
            "approval_count": int(parent.approval_count),
            "required_approvals": int(parent.required_approvals),
        }
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)


def _owned_parent(operation_id: str, actor: str) -> Any:
    parent = frappe.get_doc("Bulk Operation", operation_id)
    if parent.requested_by != actor and "Controller Admin" not in frappe.get_roles(actor):
        frappe.throw(
            "only the requester or Controller Admin may control this bulk operation",
            frappe.PermissionError,
        )
    return parent


@frappe.whitelist(methods=["POST"])
def pause_bulk_operation(bulk_operation_id: str) -> Mapping[str, Any]:
    actor = _require_author()
    operation_id = _endpoint_uuid(bulk_operation_id, "bulk operation id")
    parent = _owned_parent(operation_id, actor)
    if parent.state == "paused":
        return {"bulk_operation_id": operation_id, "state": "paused"}
    if parent.state not in {"approved", "canary", "running"}:
        frappe.throw("bulk operation is not pausable", frappe.ValidationError)
    parent.pause_requested = 1
    parent.pause_requested_by = actor
    parent.pause_requested_at = now_datetime()
    parent.save(ignore_permissions=True)
    return {"bulk_operation_id": operation_id, "state": parent.state, "pause_requested": True}


@frappe.whitelist(methods=["POST"])
def resume_bulk_operation(bulk_operation_id: str) -> Mapping[str, Any]:
    actor = _require_author()
    operation_id = _endpoint_uuid(bulk_operation_id, "bulk operation id")
    _require_bulk_parent(operation_id)
    parent = _owned_parent(operation_id, actor)
    if parent.state != "paused":
        frappe.throw("bulk operation is not paused", frappe.ValidationError)
    canaries = frappe.get_all(
        "Bulk Operation Target",
        filters={"bulk_operation": operation_id, "wave": 0},
        pluck="state",
        limit_page_length=100,
    )
    terminal = {
        "succeeded", "failed", "cancelled", "timed_out", "dead_letter",
        "needs_intervention", "rejected",
    }
    parent.pause_requested = 0
    parent.state = "running" if canaries and all(state in terminal for state in canaries) else "canary"
    parent.save(ignore_permissions=True)
    return {"bulk_operation_id": operation_id, "state": parent.state, "pause_requested": False}


@frappe.whitelist(methods=["POST"])
def cancel_bulk_operation(bulk_operation_id: str) -> Mapping[str, Any]:
    actor = _require_author()
    operation_id = _endpoint_uuid(bulk_operation_id, "bulk operation id")
    parent = _owned_parent(operation_id, actor)
    if parent.state in {"cancelled", "partial"}:
        return {"bulk_operation_id": operation_id, "state": parent.state}
    if parent.cancel_requested:
        return {
            "bulk_operation_id": operation_id,
            "state": parent.state,
            "cancel_requested": True,
        }
    if parent.state not in {"approved", "canary", "running", "paused"}:
        frappe.throw("bulk operation is not cancellable", frappe.ValidationError)
    parent.cancel_requested = 1
    parent.cancel_requested_by = actor
    parent.cancel_requested_at = now_datetime()
    parent.save(ignore_permissions=True)
    return {
        "bulk_operation_id": operation_id,
        "state": parent.state,
        "cancel_requested": True,
    }


@frappe.whitelist(methods=["GET"])
def bulk_operation_progress(
    bulk_operation_id: str, offset: int = 0, page_length: int = 50
) -> Mapping[str, Any]:
    actor = frappe.session.user
    if not actor or actor == "Guest" or not (_READ_ROLES & set(frappe.get_roles(actor))):
        frappe.throw("controller read role is required", frappe.PermissionError)
    operation_id = _endpoint_uuid(bulk_operation_id, "bulk operation id")
    try:
        offset = int(offset)
        page_length = int(page_length)
    except (TypeError, ValueError):
        frappe.throw("bulk progress pagination is invalid", frappe.ValidationError)
    if not 0 <= offset <= 1000 or not 1 <= page_length <= 100:
        frappe.throw("bulk progress pagination is invalid", frappe.ValidationError)
    parent = frappe.db.get_value(
        "Bulk Operation", operation_id,
        [
            "state", "approval_status", "approval_count", "required_approvals",
            "total_targets", "planned_count", "queued_count", "running_count",
            "succeeded_count", "failed_count", "cancelled_count",
            "needs_intervention_count", "pause_requested", "cancel_requested",
            "pause_requested_by", "pause_requested_at", "cancel_requested_by",
            "cancel_requested_at",
            "snapshot_hash", "dry_run_hash",
        ],
        as_dict=True,
    )
    if not parent:
        frappe.throw("bulk operation does not exist", frappe.DoesNotExistError)
    targets = frappe.get_all(
        "Bulk Operation Target",
        filters={"bulk_operation": operation_id},
        fields=[
            "target_key", "ordinal", "wave", "attempt", "server_agent", "bench",
            "managed_site", "environment_snapshot", "state", "child_operation",
            "error_code", "started_at", "completed_at",
        ],
        order_by="ordinal asc, attempt asc",
        start=offset,
        page_length=page_length,
    )
    return {
        "bulk_operation_id": operation_id,
        "state": parent.state,
        "approval_status": parent.approval_status,
        "approval_count": int(parent.approval_count),
        "required_approvals": int(parent.required_approvals),
        "counts": {
            "total": int(parent.total_targets),
            "planned": int(parent.planned_count),
            "queued": int(parent.queued_count),
            "running": int(parent.running_count),
            "succeeded": int(parent.succeeded_count),
            "failed": int(parent.failed_count),
            "cancelled": int(parent.cancelled_count),
            "needs_intervention": int(parent.needs_intervention_count),
        },
        "pause_requested": bool(parent.pause_requested),
        "pause_requested_by": parent.pause_requested_by,
        "pause_requested_at": parent.pause_requested_at,
        "cancel_requested": bool(parent.cancel_requested),
        "cancel_requested_by": parent.cancel_requested_by,
        "cancel_requested_at": parent.cancel_requested_at,
        "snapshot_hash": parent.snapshot_hash,
        "dry_run_hash": parent.dry_run_hash,
        "offset": offset,
        "page_length": page_length,
        "targets": [dict(row) for row in targets],
    }


__all__ = [
    "bulk_operation_progress", "cancel_bulk_operation", "create_bulk_data_update",
    "decide_bulk_operation", "pause_bulk_operation", "resume_bulk_operation",
    "retry_failed_bulk_data_update",
]
