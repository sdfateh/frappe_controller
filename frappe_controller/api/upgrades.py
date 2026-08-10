"""Authenticated controller-owned drain and out-of-band upgrade workflow."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

import frappe
from frappe.utils import now_datetime

from ..operation_service import OperationAuthoringError
from ..feature_flags import require_feature


_REQUEST_FIELDS = frozenset({
    "agent_id", "desired_agent_version", "desired_image_digest",
    "previous_image_digest",
})
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FAILURES = frozenset({
    "deployment_failed", "health_timeout", "operator_requested", "version_mismatch",
})


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently accepting the last one."""
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _admin() -> str:
    actor = frappe.session.user
    if not actor or actor == "Guest" or "Controller Admin" not in frappe.get_roles(actor):
        frappe.throw("Controller Admin role is required", frappe.PermissionError)
    return actor


def _identity(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise OperationAuthoringError(f"{label} is invalid")
    return value


def _agent(agent_id: str) -> Any:
    if not isinstance(agent_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", agent_id):
        frappe.throw("agent id is invalid", frappe.ValidationError)
    if not frappe.db.exists("Server Agent", agent_id):
        frappe.throw("server agent does not exist", frappe.DoesNotExistError)
    return frappe.get_doc("Server Agent", agent_id)


def _save(document: Any) -> None:
    document.flags.controller_service = True
    document.save(ignore_permissions=True)


def _counts(agent_name: str) -> dict[str, int]:
    return {
        state: int(frappe.db.count("Operation", {"server_agent": agent_name, "state": state}))
        for state in ("queued", "leased", "running")
    }


def _has_active_operations(counts: Mapping[str, int]) -> bool:
    return counts["leased"] + counts["running"] > 0


def _healthy_post_deployment_heartbeat(document: Any, expected_version: str) -> bool:
    """Require a healthy inventory heartbeat produced by the deployed generation."""
    return bool(
        document.status == "Online"
        and document.reported_status == "ready"
        and document.agent_version == expected_version
        and document.upgrade_deployed_at
        and document.last_seen
        and document.last_seen >= document.upgrade_deployed_at
    )


@frappe.whitelist(methods=["POST"])
def request_agent_upgrade(request_json: str) -> Mapping[str, Any]:
    actor = _admin()
    try:
        if not isinstance(request_json, str) or len(request_json.encode("utf-8")) > 4096:
            raise OperationAuthoringError("upgrade request is invalid")
        try:
            value = json.loads(
                request_json, object_pairs_hook=_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            raise OperationAuthoringError("upgrade request is invalid JSON") from None
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise OperationAuthoringError("upgrade request fields do not match the contract")
        desired_version = _identity(value["desired_agent_version"], "desired version", _VERSION)
        desired_digest = _identity(value["desired_image_digest"], "desired image digest", _DIGEST)
        previous_digest = _identity(value["previous_image_digest"], "previous image digest", _DIGEST)
        document = _agent(value["agent_id"])
        require_feature(frappe, "agent_upgrades", document.environment)
        if document.upgrade_state not in {"none", "succeeded", "rolled_back", "cancelled"}:
            raise OperationAuthoringError("agent already has an active upgrade")
        if (
            not document.enabled
            or document.status != "Online"
            or document.reported_status != "ready"
            or not _VERSION.fullmatch(document.agent_version or "")
        ):
            raise OperationAuthoringError("agent is not online and healthy with a verified current version")
        if document.observed_image_digest and document.observed_image_digest != previous_digest:
            raise OperationAuthoringError("previous image digest differs from the last verified deployment")
        if desired_version == document.agent_version or desired_digest == previous_digest:
            raise OperationAuthoringError("upgrade must change both version and image digest")
        requested_at = now_datetime()
        document.drain_requested = 1
        document.drain_requested_by = actor
        document.drain_requested_at = requested_at
        document.upgrade_state = "planned"
        document.desired_agent_version = desired_version
        document.desired_image_digest = desired_digest
        document.previous_agent_version = document.agent_version
        document.previous_image_digest = previous_digest
        document.upgrade_requested_by = actor
        document.upgrade_requested_at = requested_at
        document.upgrade_deployed_at = None
        document.upgrade_verified_at = None
        document.upgrade_failure_code = None
        _save(document)
        return {"agent_id": document.agent_id, "upgrade_state": document.upgrade_state, "drain_requested": True}
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def refresh_agent_drain(agent_id: str) -> Mapping[str, Any]:
    _admin()
    document = _agent(agent_id)
    if document.upgrade_state not in {"planned", "draining", "ready_to_deploy"}:
        frappe.throw("agent upgrade is not draining", frappe.ValidationError)
    counts = _counts(document.name)
    desired_state = "draining" if _has_active_operations(counts) else "ready_to_deploy"
    if document.upgrade_state != desired_state:
        document.upgrade_state = desired_state
        _save(document)
    return {"agent_id": document.agent_id, "upgrade_state": document.upgrade_state, "operation_counts": counts}


@frappe.whitelist(methods=["POST"])
def confirm_agent_deployment(agent_id: str, observed_image_digest: str) -> Mapping[str, Any]:
    _admin()
    document = _agent(agent_id)
    try:
        observed = _identity(observed_image_digest, "observed image digest", _DIGEST)
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)
    if document.upgrade_state != "ready_to_deploy" or observed != document.desired_image_digest:
        frappe.throw("deployment does not match the ready upgrade", frappe.PermissionError)
    if _has_active_operations(_counts(document.name)):
        frappe.throw("agent acquired active operations after drain verification", frappe.ValidationError)
    document.observed_image_digest = observed
    document.upgrade_deployed_at = now_datetime()
    document.upgrade_state = "verifying"
    _save(document)
    return {"agent_id": document.agent_id, "upgrade_state": document.upgrade_state}


@frappe.whitelist(methods=["POST"])
def request_agent_rollback(agent_id: str, failure_code: str) -> Mapping[str, Any]:
    _admin()
    document = _agent(agent_id)
    valid_failure = (
        document.upgrade_state == "verifying" and failure_code in _FAILURES
    ) or (
        document.upgrade_state == "ready_to_deploy" and failure_code == "deployment_failed"
    )
    if not valid_failure:
        frappe.throw("rollback request is invalid", frappe.ValidationError)
    document.upgrade_failure_code = failure_code
    document.upgrade_state = "rollback_required"
    _save(document)
    return {
        "agent_id": document.agent_id,
        "upgrade_state": document.upgrade_state,
        "previous_agent_version": document.previous_agent_version,
        "previous_image_digest": document.previous_image_digest,
    }


@frappe.whitelist(methods=["POST"])
def cancel_agent_upgrade(agent_id: str) -> Mapping[str, Any]:
    """Release a pre-deployment drain after an explicit operator cancellation."""
    _admin()
    document = _agent(agent_id)
    if document.upgrade_state not in {"planned", "draining", "ready_to_deploy"}:
        frappe.throw("only a pre-deployment upgrade may be cancelled", frappe.ValidationError)
    document.upgrade_state = "cancelled"
    document.upgrade_failure_code = "operator_requested"
    document.drain_requested = 0
    # Frappe's tracked Version row attributes this service-owned transition to
    # the authenticated administrator; the original request fields are retained.
    _save(document)
    return {
        "agent_id": document.agent_id,
        "upgrade_state": document.upgrade_state,
        "drain_requested": False,
    }


@frappe.whitelist(methods=["POST"])
def confirm_agent_rollback(agent_id: str, observed_image_digest: str) -> Mapping[str, Any]:
    _admin()
    document = _agent(agent_id)
    try:
        observed = _identity(observed_image_digest, "observed image digest", _DIGEST)
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)
    if document.upgrade_state != "rollback_required" or observed != document.previous_image_digest:
        frappe.throw("rollback deployment does not match the recorded prior image", frappe.PermissionError)
    if _has_active_operations(_counts(document.name)):
        frappe.throw("agent has active operations during rollback", frappe.ValidationError)
    document.observed_image_digest = observed
    document.upgrade_deployed_at = now_datetime()
    document.upgrade_state = "rollback_verifying"
    _save(document)
    return {"agent_id": document.agent_id, "upgrade_state": document.upgrade_state}


@frappe.whitelist(methods=["POST"])
def verify_agent_deployment(agent_id: str) -> Mapping[str, Any]:
    """Close an upgrade only after a healthy heartbeat from the expected version.

    The image digest is bound by the out-of-band deployment confirmation while
    the version and health are controller observations from inventory heartbeat
    processing.  Keeping the drain asserted until this method succeeds prevents
    unverified generations from receiving new commands.
    """
    _admin()
    document = _agent(agent_id)
    if document.upgrade_state == "verifying":
        expected_version = document.desired_agent_version
        expected_digest = document.desired_image_digest
        completed_state = "succeeded"
    elif document.upgrade_state == "rollback_verifying":
        expected_version = document.previous_agent_version
        expected_digest = document.previous_image_digest
        completed_state = "rolled_back"
    else:
        frappe.throw("agent deployment is not awaiting verification", frappe.ValidationError)
    if document.observed_image_digest != expected_digest:
        frappe.throw("observed image digest does not match the expected deployment", frappe.PermissionError)
    if _has_active_operations(_counts(document.name)):
        frappe.throw("agent has active operations during deployment verification", frappe.ValidationError)
    if not _healthy_post_deployment_heartbeat(document, expected_version):
        frappe.throw("a healthy post-deployment heartbeat from the expected version is required", frappe.ValidationError)
    document.upgrade_state = completed_state
    document.upgrade_verified_at = now_datetime()
    document.drain_requested = 0
    _save(document)
    return {
        "agent_id": document.agent_id,
        "upgrade_state": document.upgrade_state,
        "drain_requested": False,
    }


@frappe.whitelist(methods=["GET"])
def agent_upgrade_status(agent_id: str) -> Mapping[str, Any]:
    actor = frappe.session.user
    roles = set(frappe.get_roles(actor)) if actor and actor != "Guest" else set()
    if not ({"Controller Admin", "Auditor"} & roles):
        frappe.throw("Controller Admin or Auditor role is required", frappe.PermissionError)
    document = _agent(agent_id)
    return {
        "agent_id": document.agent_id,
        "agent_version": document.agent_version,
        "drain_requested": bool(document.drain_requested),
        "upgrade_state": document.upgrade_state,
        "desired_agent_version": document.desired_agent_version,
        "desired_image_digest": document.desired_image_digest,
        "previous_agent_version": document.previous_agent_version,
        "previous_image_digest": document.previous_image_digest,
        "observed_image_digest": document.observed_image_digest,
        "upgrade_requested_by": document.upgrade_requested_by,
        "upgrade_requested_at": document.upgrade_requested_at,
        "upgrade_deployed_at": document.upgrade_deployed_at,
        "upgrade_verified_at": document.upgrade_verified_at,
        "upgrade_failure_code": document.upgrade_failure_code,
        "operation_counts": _counts(document.name),
    }


__all__ = [
    "agent_upgrade_status", "cancel_agent_upgrade", "confirm_agent_deployment", "confirm_agent_rollback",
    "refresh_agent_drain", "request_agent_rollback", "request_agent_upgrade",
    "verify_agent_deployment",
]
