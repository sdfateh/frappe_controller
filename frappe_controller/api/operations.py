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
from ..operation_retry import validate_retry_source
from ..creation_readiness import check_creation_readiness
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


def _check_blank_creation(operation_type: str, server_agent: str, bench: str, payload: Mapping[str, Any]) -> None:
    if operation_type != "site.create_blank":
        return
    normalized = validate_lifecycle_payload(operation_type, payload).normalized_payload
    readiness = check_creation_readiness(frappe, domain=normalized["domain"], server_agent=server_agent, bench=bench)
    if not readiness["ready"]:
        failures = "; ".join(row["label"] + ": " + row["message"] for row in readiness["checks"] if not row["passed"])
        frappe.throw("Site creation readiness failed: " + failures, frappe.ValidationError)


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
        # Existing immutable authoring requests retain their idempotent response.
        if not frappe.db.exists("Operation", request.operation_id):
            _check_blank_creation(request.operation_type, request.server_agent, request.bench, request.payload)
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
            "managed_site", "bench",
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
    _check_blank_creation(row.operation_type, row.server_agent, row.bench, json.loads(row.payload_json))
    FrappeCommandStore(
        frappe, command_lifetime_seconds=_command_lifetime()
    ).enqueue_approved_operation(canonical, now=datetime.now(UTC))
    return {"operation_id": canonical, "state": "queued"}


@frappe.whitelist(methods=["POST"])
def retry_operation(operation_id: str, recovery_confirmed: Any = False) -> Mapping[str, Any]:
    """Create one successor with fresh authorization; never reopen an old command."""
    actor = _require_author()
    original = frappe.get_doc("Operation", operation_id)
    original.check_permission("read")
    if original.requested_by != actor and "Controller Admin" not in frappe.get_roles(actor):
        frappe.throw("Only the requester or Controller Admin may retry an operation", frappe.PermissionError)
    # Customer creation/reconciliation locks Customer first. Preserve that order
    # to avoid deadlocks and retain one tracked creation lineage per Customer.
    customers = frappe.db.sql(
        "SELECT name FROM `tabCustomer` WHERE controller_site_creation_operation=%s ORDER BY name FOR UPDATE",
        (original.name,), as_dict=True,
    )
    frappe.db.sql("SELECT name FROM `tabOperation` WHERE name=%s FOR UPDATE", (original.name,))
    original.reload()
    try:
        validate_retry_source(original, recovery_confirmed=recovery_confirmed in (True, 1, "1"))
        existing = frappe.db.sql(
            "SELECT name,state FROM `tabOperation` WHERE retry_of=%s AND IFNULL(bulk_parent,'')='' "
            "ORDER BY creation LIMIT 1 FOR UPDATE", (original.name,), as_dict=True,
        )
        if existing:
            return {"operation_id": existing[0].name, "state": existing[0].state}
        payload = json.loads(original.payload_json)
        if original.operation_type in {"site.create", "site.create_blank", "site.create_from_backup"}:
            if frappe.db.exists("Managed Site", {"domain": payload.get("domain")}):
                frappe.throw("The site is already in inventory; inspect it instead of recreating it", frappe.ValidationError)
        require_operation(
            frappe, environment=target_environment(frappe, original.server_agent, original.managed_site),
            operation_type=original.operation_type, payload_json=original.payload_json,
        )
        _check_blank_creation(original.operation_type, original.server_agent, original.bench, payload)
        customer_docs = [frappe.get_doc("Customer", row.name) for row in customers]
        for customer in customer_docs:
            customer.check_permission("write")
            if customer.controller_production_managed_site:
                frappe.throw("Customer already has a Managed Site", frappe.ValidationError)
        authored = OperationAuthoringService(
            FrappeOperationAuthoringRepository(frappe), validate_lifecycle_payload,
        ).author(OperationRequest(
            operation_id=str(uuid.uuid4()), operation_type=original.operation_type,
            server_agent=original.server_agent, bench=original.bench,
            managed_site=original.managed_site, payload=payload, retry_of=original.name,
        ), actor=actor)
        from .customer_sites import _set_site_operation

        for customer in customer_docs:
            _set_site_operation(customer, authored.operation_id)
        state = authored.state
        if authored.required_approvals == 0:
            FrappeCommandStore(frappe, command_lifetime_seconds=_command_lifetime()).enqueue_approved_operation(
                authored.operation_id, now=datetime.now(UTC),
            )
            state = "queued"
        return {"operation_id": authored.operation_id, "state": state}
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)


__all__ = ["create_operation", "start_operation", "retry_operation"]
