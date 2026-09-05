"""Certificate-authenticated Agent upgrade coordination."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

import frappe
from frappe.utils import now_datetime
from werkzeug.wrappers import Response

from ..frappe_security_store import FrappeCertificateStore
from ..agent_request_auth import trusted_peer_and_route_from_frappe_request
from ..security import ControllerRequestError, canonical_json, require_agent_id

_FIELDS = frozenset({"agent_id", "action", "observed_image_digest"})
_ACTIONS = frozenset({"poll", "deployed", "failed", "verify"})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _response(value: Mapping[str, Any], status: int = 200) -> Response:
    return Response(
        canonical_json(value), status=status,
        content_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _body() -> dict[str, Any]:
    if frappe.request.method != "POST" or frappe.request.mimetype != "application/json":
        raise ControllerRequestError("invalid_upgrade_request", 415)
    raw = frappe.request.get_data(cache=True)
    if len(raw) > 4096:
        raise ControllerRequestError("invalid_upgrade_request", 413)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ControllerRequestError("invalid_upgrade_request", 400) from None
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ControllerRequestError("invalid_upgrade_request", 400)
    try:
        require_agent_id(value["agent_id"])
    except (KeyError, ValueError):
        raise ControllerRequestError("invalid_agent", 403) from None
    if value["action"] not in _ACTIONS or not isinstance(value["observed_image_digest"], str):
        raise ControllerRequestError("invalid_upgrade_request", 400)
    return value


def _authenticate(agent_id: str) -> None:
    peer, routed = trusted_peer_and_route_from_frappe_request(frappe.request, "upgrade")
    FrappeCertificateStore.from_environment(
        frappe_module=frappe
    ).authenticate_peer(peer, routed, agent_id)


def _counts(name: str) -> dict[str, int]:
    return {
        state: int(frappe.db.count("Operation", {"server_agent": name, "state": state}))
        for state in ("queued", "leased", "running")
    }


def _save(document: Any) -> None:
    document.flags.controller_service = True
    document.save(ignore_permissions=True)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def upgrade_route(**_request_arguments: Any) -> Response:
    try:
        value = _body()
        agent_id = value["agent_id"]
        _authenticate(agent_id)
        document = frappe.get_doc("Server Agent", agent_id)
        action = value["action"]
        observed = value["observed_image_digest"]
        counts = _counts(document.name)

        if action == "poll":
            if _DIGEST.fullmatch(observed):
                stable = {"none", "succeeded", "rolled_back", "cancelled"}
                if not document.observed_image_digest and document.upgrade_state in stable:
                    document.observed_image_digest = observed
                    _save(document)
                elif document.upgrade_state in stable and document.observed_image_digest != observed:
                    raise ControllerRequestError("image_digest_mismatch", 409)
            if document.upgrade_state in {"planned", "draining", "ready_to_deploy"}:
                desired = "draining" if counts["leased"] + counts["running"] else "ready_to_deploy"
                if document.upgrade_state != desired:
                    document.upgrade_state = desired
                    _save(document)
            if document.upgrade_state == "ready_to_deploy":
                instruction = {"action": "deploy", "image_digest": document.desired_image_digest}
            elif document.upgrade_state == "rollback_required":
                instruction = {"action": "rollback", "image_digest": document.previous_image_digest}
            elif document.upgrade_state in {"verifying", "rollback_verifying"}:
                instruction = {"action": "verify", "image_digest": document.observed_image_digest}
            else:
                instruction = {"action": "none", "image_digest": ""}
            return _response({"accepted": True, "upgrade_state": document.upgrade_state, **instruction})

        if not _DIGEST.fullmatch(observed):
            raise ControllerRequestError("invalid_image_digest", 400)
        if action == "failed":
            if document.upgrade_state not in {"ready_to_deploy", "verifying"}:
                raise ControllerRequestError("upgrade_state_conflict", 409)
            document.upgrade_failure_code = "deployment_failed"
            document.upgrade_state = "rollback_required"
            _save(document)
            return _response({"accepted": True, "upgrade_state": document.upgrade_state})

        if action == "deployed":
            if counts["leased"] + counts["running"]:
                raise ControllerRequestError("upgrade_not_drained", 409)
            choices = {
                "ready_to_deploy": (document.desired_image_digest, "verifying"),
                "rollback_required": (document.previous_image_digest, "rollback_verifying"),
            }
            if document.upgrade_state not in choices or observed != choices[document.upgrade_state][0]:
                raise ControllerRequestError("upgrade_state_conflict", 409)
            document.observed_image_digest = observed
            document.upgrade_deployed_at = now_datetime()
            document.upgrade_state = choices[document.upgrade_state][1]
            _save(document)
            return _response({"accepted": True, "upgrade_state": document.upgrade_state})

        expected = (
            document.desired_image_digest
            if document.upgrade_state == "verifying"
            else document.previous_image_digest
        )
        expected_version = (
            document.desired_agent_version
            if document.upgrade_state == "verifying"
            else document.previous_agent_version
        )
        if (
            document.upgrade_state not in {"verifying", "rollback_verifying"}
            or observed != expected
            or document.observed_image_digest != expected
            or counts["leased"] + counts["running"]
            or document.status != "Online"
            or document.reported_status != "ready"
            or document.agent_version != expected_version
            or not document.last_seen
            or document.last_seen < document.upgrade_deployed_at
        ):
            raise ControllerRequestError("upgrade_verification_pending", 409)
        document.upgrade_state = "succeeded" if document.upgrade_state == "verifying" else "rolled_back"
        document.upgrade_verified_at = now_datetime()
        document.drain_requested = 0
        _save(document)
        return _response({"accepted": True, "upgrade_state": document.upgrade_state})
    except ControllerRequestError as error:
        frappe.db.rollback()
        return _response({"accepted": False, "error": error.code}, error.status)
    except Exception:
        frappe.db.rollback()
        return _response({"accepted": False, "error": "internal_error"}, 500)
