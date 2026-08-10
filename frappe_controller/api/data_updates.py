"""Authenticated preview and approval-bound promotion for fixed data updates."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Mapping

import frappe

from ..data_update_authoring import (
    canonical_hash,
    data_update_authorization,
    normalize_data_update_command,
)
from ..frappe_operation_service import FrappeOperationAuthoringRepository
from ..frappe_store import FrappeCommandStore
from ..feature_flags import require_feature, target_environment
from ..operation_service import OperationAuthoringError, OperationAuthoringService, OperationRequest
from .operations import _command_lifetime, _pairs, _require_author


_PREVIEW_FIELDS = frozenset({
    "operation_id", "operation_type", "server_agent", "bench", "managed_site",
    "policy_id", "payload",
})
_RESULT_FIELDS = frozenset({
    "contract_version", "operation_id", "operation", "policy_id", "policy_version",
    "payload_hash", "actor", "reason", "target", "dry_run", "maximum_rows",
    "matched_count", "affected_count", "result", "evidence", "evidence_truncated",
})


def _json_request(raw: str, expected: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > 256 * 1024:
        raise OperationAuthoringError("data update request is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise OperationAuthoringError("data update request is invalid JSON") from None
    if not isinstance(value, Mapping) or set(value) != expected:
        raise OperationAuthoringError("data update request fields do not match the contract")
    return value


def _request_identity(value: Mapping[str, Any]) -> None:
    for field in ("operation_id", "operation_type", "server_agent", "bench", "managed_site", "policy_id"):
        if not isinstance(value[field], str) or not value[field] or value[field] != value[field].strip():
            raise OperationAuthoringError("data update request identity is invalid")
    try:
        operation_id = str(uuid.UUID(value["operation_id"]))
    except (ValueError, TypeError, AttributeError):
        raise OperationAuthoringError("operation id must be a UUID") from None
    if operation_id != value["operation_id"].lower():
        raise OperationAuthoringError("operation id must be canonical")


def validate_preview_result(
    result_json: str,
    *,
    preview: Mapping[str, Any],
    command: Mapping[str, Any],
    target: Mapping[str, Any],
) -> None:
    try:
        outer = json.loads(result_json)
        result = outer["result"]
    except (TypeError, KeyError, json.JSONDecodeError):
        raise OperationAuthoringError("preview result is invalid") from None
    if outer.get("status") != "succeeded" or not isinstance(result, Mapping) or set(result) != _RESULT_FIELDS:
        raise OperationAuthoringError("preview result is invalid")
    payload = command["payload"]
    expected_target = {
        "agent_id": target["agent_id"],
        "bench_id": target["bench_id"],
        "site_id": target["site_domain"],
        "site_domain": target["site_domain"],
        "doctype": payload["doctype"],
    }
    if (
        result.get("contract_version") != "1.0"
        or result.get("operation_id") != preview["operation_id"]
        or result.get("operation") != preview["operation_type"]
        or result.get("policy_id") != command["policy_id"]
        or result.get("policy_version") != "1.0"
        or result.get("payload_hash") != canonical_hash(payload)
        or result.get("actor") != preview["requested_by"]
        or result.get("reason") != payload["reason"]
        or result.get("target") != expected_target
        or result.get("dry_run") is not True
        or result.get("maximum_rows") != payload["maximum_rows"]
    ):
        raise OperationAuthoringError("preview result identity does not match the request")
    matched = result.get("matched_count")
    affected = result.get("affected_count")
    evidence = result.get("evidence")
    if (
        type(matched) is not int or type(affected) is not int
        or not 0 <= affected <= matched <= payload["maximum_rows"]
        or result.get("result") not in {"would_update", "no_changes"}
        or not isinstance(evidence, list) or len(evidence) > 20
        or type(result.get("evidence_truncated")) is not bool
    ):
        raise OperationAuthoringError("preview result counts or evidence are invalid")


@frappe.whitelist(methods=["POST"])
def preview_data_update(request_json: str) -> Mapping[str, Any]:
    actor = _require_author()
    try:
        value = _json_request(request_json, _PREVIEW_FIELDS)
        _request_identity(value)
        require_feature(
            frappe,
            "data_update_preview",
            target_environment(
                frappe, value["server_agent"], value["managed_site"]
            ),
        )
        command = normalize_data_update_command(
            value["operation_type"],
            {"policy_id": value["policy_id"], "payload": value["payload"]},
            dry_run=True,
        )
        authored = OperationAuthoringService(
            FrappeOperationAuthoringRepository(frappe),
            lambda operation, payload: data_update_authorization(operation, payload, preview=True),
        ).author(OperationRequest(
            operation_id=value["operation_id"],
            operation_type=value["operation_type"],
            server_agent=value["server_agent"],
            bench=value["bench"],
            managed_site=value["managed_site"],
            payload=command,
        ), actor=actor)
        FrappeCommandStore(
            frappe, command_lifetime_seconds=_command_lifetime()
        ).enqueue_approved_operation(authored.operation_id, now=datetime.now(UTC))
        return {
            "preview_operation_id": authored.operation_id,
            "state": "queued",
            "payload_hash": authored.payload_hash,
            "inventory_revision": authored.inventory_revision,
        }
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def promote_data_update(preview_operation_id: str, operation_id: str) -> Mapping[str, Any]:
    actor = _require_author()
    try:
        preview_uuid = str(uuid.UUID(preview_operation_id))
        actual_uuid = str(uuid.UUID(operation_id))
    except (ValueError, TypeError, AttributeError):
        frappe.throw("operation ids must be UUIDs", frappe.ValidationError)
    if preview_uuid != preview_operation_id.lower() or actual_uuid != operation_id.lower():
        frappe.throw("operation ids must be canonical", frappe.ValidationError)
    preview = frappe.db.get_value(
        "Operation", preview_uuid,
        [
            "operation_id", "operation_type", "server_agent", "bench", "managed_site",
            "requested_by", "state", "payload_json", "result_json", "result_hash",
        ],
        as_dict=True,
    )
    target_row = frappe.db.get_value(
        "Operation Target", {"operation": preview_uuid},
        ["inventory_revision_snapshot"], as_dict=True,
    )
    if not preview or not target_row or preview.requested_by != actor:
        frappe.throw("only the preview requester may promote it", frappe.PermissionError)
    require_feature(
        frappe,
        "data_update_apply",
        target_environment(
            frappe, preview.server_agent, preview.managed_site
        ),
    )
    if preview.state != "succeeded" or not preview.result_hash or not preview.result_json:
        frappe.throw("data update preview has not succeeded", frappe.ValidationError)
    try:
        raw_command = json.loads(preview.payload_json)
        preview_command = normalize_data_update_command(
            preview.operation_type, raw_command, dry_run=True
        )
        repository = FrappeOperationAuthoringRepository(frappe)
        current = repository.resolve_target(
            preview.server_agent, preview.bench, preview.managed_site
        )
        if current.inventory_revision != target_row.inventory_revision_snapshot:
            raise OperationAuthoringError("inventory changed after the preview")
        validate_preview_result(
            preview.result_json,
            preview=dict(preview),
            command=preview_command,
            target={
                "agent_id": current.agent_id,
                "bench_id": current.bench_id,
                "site_domain": current.site_domain,
            },
        )
        apply_command = {
            **preview_command,
            "payload": {**preview_command["payload"], "dry_run": False},
        }
        authored = OperationAuthoringService(
            repository,
            lambda operation, payload: data_update_authorization(operation, payload, preview=False),
        ).author(OperationRequest(
            operation_id=actual_uuid,
            operation_type=preview.operation_type,
            server_agent=preview.server_agent,
            bench=preview.bench,
            managed_site=preview.managed_site,
            payload=apply_command,
            preview_of=preview_uuid,
            preview_result_hash=preview.result_hash,
        ), actor=actor)
        return {
            "operation_id": authored.operation_id,
            "preview_operation_id": preview_uuid,
            "state": authored.state,
            "approval_status": authored.approval_status,
            "required_approvals": authored.required_approvals,
            "payload_hash": authored.payload_hash,
            "preview_result_hash": authored.preview_result_hash,
        }
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)


__all__ = ["preview_data_update", "promote_data_update", "validate_preview_result"]
