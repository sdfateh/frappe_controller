"""Authenticated operator endpoints for safely authored lifecycle operations."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Mapping

import frappe

from ..frappe_operation_service import FrappeOperationAuthoringRepository
from ..frappe_store import FrappeCommandStore
from ..feature_flags import require_operation, target_environment
from ..lifecycle_authoring import validate_lifecycle_payload
from ..operation_service import (
    OperationAuthoringError,
    OperationAuthoringService,
    OperationRequest,
)


_AUTHOR_ROLES = frozenset({"Controller Admin", "Operator"})
_REQUEST_FIELDS = frozenset({
    "operation_id", "operation_type", "server_agent", "bench", "managed_site", "payload",
})


def _require_author() -> str:
    actor = frappe.session.user
    if not actor or actor == "Guest" or not (_AUTHOR_ROLES & set(frappe.get_roles(actor))):
        frappe.throw("Controller Admin or Operator role is required", frappe.PermissionError)
    return actor


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OperationAuthoringError("operation request contains duplicate fields")
        result[key] = value
    return result


def _request(request_json: str) -> OperationRequest:
    if not isinstance(request_json, str) or len(request_json.encode("utf-8")) > 256 * 1024:
        raise OperationAuthoringError("operation request is invalid")
    try:
        value = json.loads(request_json, object_pairs_hook=_pairs, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise OperationAuthoringError("operation request is invalid JSON") from None
    if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
        raise OperationAuthoringError("operation request fields do not match the contract")
    for field in ("operation_id", "operation_type", "server_agent", "bench"):
        if not isinstance(value[field], str) or not value[field] or value[field] != value[field].strip():
            raise OperationAuthoringError("operation request identity is invalid")
    if value["managed_site"] is not None and (
        not isinstance(value["managed_site"], str) or not value["managed_site"]
        or value["managed_site"] != value["managed_site"].strip()
    ):
        raise OperationAuthoringError("managed site identity is invalid")
    if not isinstance(value["payload"], Mapping):
        raise OperationAuthoringError("operation payload must be an object")
    return OperationRequest(**value)


def _command_lifetime() -> int:
    value = frappe.conf.get("frappe_controller_command_lifetime_seconds", 300)
    if type(value) is not int or not 1 <= value <= 300:
        raise RuntimeError("invalid frappe_controller_command_lifetime_seconds configuration")
    return value


@frappe.whitelist(methods=["POST"])
def create_operation(request_json: str) -> Mapping[str, Any]:
    actor = _require_author()
    try:
        request = _request(request_json)
        require_operation(
            frappe,
            environment=target_environment(
                frappe, request.server_agent, request.managed_site
            ),
            operation_type=request.operation_type,
            payload_json=json.dumps(
                request.payload, sort_keys=True, separators=(",", ":")
            ),
        )
        authored = OperationAuthoringService(
            FrappeOperationAuthoringRepository(frappe), validate_lifecycle_payload
        ).author(request, actor=actor)
        state = authored.state
        if authored.required_approvals == 0:
            FrappeCommandStore(
                frappe, command_lifetime_seconds=_command_lifetime()
            ).enqueue_approved_operation(authored.operation_id, now=datetime.now(UTC))
            state = "queued"
        return {
            "operation_id": authored.operation_id,
            "state": state,
            "approval_status": authored.approval_status,
            "required_approvals": authored.required_approvals,
            "payload_hash": authored.payload_hash,
        }
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def start_operation(operation_id: str) -> Mapping[str, Any]:
    actor = _require_author()
    try:
        canonical = str(uuid.UUID(operation_id))
    except (ValueError, TypeError, AttributeError):
        frappe.throw("operation id must be a UUID", frappe.ValidationError)
    if canonical != operation_id.lower():
        frappe.throw("operation id must be canonical", frappe.ValidationError)
    row = frappe.db.get_value(
        "Operation", canonical,
        [
            "requested_by", "approval_status", "required_approvals", "state",
            "server_agent", "operation_type", "payload_json", "bulk_parent",
            "managed_site",
        ],
        as_dict=True,
    )
    if not row:
        frappe.throw("operation does not exist", frappe.DoesNotExistError)
    if row.requested_by != actor and "Controller Admin" not in frappe.get_roles(actor):
        frappe.throw("only the requester or Controller Admin may start an operation", frappe.PermissionError)
    require_operation(
        frappe,
        environment=target_environment(
            frappe, row.server_agent, row.managed_site
        ),
        operation_type=row.operation_type,
        payload_json=row.payload_json,
        bulk_parent=row.bulk_parent,
    )
    if row.state == "queued":
        return {"operation_id": canonical, "state": "queued"}
    if row.approval_status not in {"approved", "not_required"}:
        frappe.throw("operation approval threshold is not satisfied", frappe.PermissionError)
    FrappeCommandStore(
        frappe, command_lifetime_seconds=_command_lifetime()
    ).enqueue_approved_operation(canonical, now=datetime.now(UTC))
    return {"operation_id": canonical, "state": "queued"}


__all__ = ["create_operation", "start_operation"]
