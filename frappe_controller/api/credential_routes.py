"""One-time signed HTTPS handoff for generated site Administrator credentials."""

from __future__ import annotations

import re
from typing import Any, Mapping

import frappe
from frappe.utils import now_datetime
from werkzeug.wrappers import Response

from ..frappe_security_store import FrappeCertificateStore
from ..agent_request_auth import trusted_peer_and_route_from_frappe_request
from ..security import ControllerRequestError, canonical_json, parse_exact_json
from .routes import _request_body

_FIELDS = frozenset({"protocol_version", "agent_id", "audience", "operation_id", "credential"})
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _response(value: Mapping[str, Any], status: int = 200) -> Response:
    return Response(
        canonical_json(value), status=status,
        content_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])
def store_credential_route(**_request_arguments: Any) -> Response:
    try:
        value = parse_exact_json(_request_body(4096), _FIELDS, limit=4096)
        agent_id = value.get("agent_id")
        operation_id = value.get("operation_id")
        credential = value.get("credential")
        if (
            value.get("protocol_version") != "1.0"
            or value.get("audience") != "frappe-controller"
            or not isinstance(agent_id, str)
            or not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id)
            or not isinstance(credential, str) or not 1 <= len(credential) <= 512
            or "\x00" in credential
        ):
            raise ControllerRequestError("invalid_credential_request", 400)
        peer, routed = trusted_peer_and_route_from_frappe_request(frappe.request, "credentials")
        FrappeCertificateStore.from_environment(frappe_module=frappe).authenticate_peer(
            peer, routed, agent_id
        )
        if not frappe.db.sql(
            "SELECT name FROM `tabOperation` WHERE name = %s FOR UPDATE",
            operation_id,
        ):
            raise ControllerRequestError("unknown_operation", 404)
        operation = frappe.get_doc("Operation", operation_id)
        if operation.server_agent != agent_id or operation.operation_type not in {
            "site.create", "site.create_blank", "site.create_from_backup",
        }:
            raise ControllerRequestError("credential_ownership_mismatch", 403)
        if operation.credential_consumed_at:
            raise ControllerRequestError("credential_already_consumed", 409)
        if operation.credential_received_at:
            if operation.get_password("administrator_credential", raise_exception=False) != credential:
                raise ControllerRequestError("credential_conflict", 409)
            return _response({"accepted": True})
        operation.administrator_credential = credential
        operation.credential_received_at = now_datetime()
        operation.flags.controller_service = True
        operation.save(ignore_permissions=True)
        return _response({"accepted": True})
    except ControllerRequestError as error:
        frappe.db.rollback()
        return _response({"accepted": False, "error": error.code}, error.status)
    except Exception:
        frappe.db.rollback()
        return _response({"accepted": False, "error": "internal_error"}, 500)


@frappe.whitelist(methods=["POST"])
def consume_operation_credential(operation_id: str) -> Response:
    user = getattr(getattr(frappe, "session", None), "user", None)
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        frappe.throw("Operation ID is invalid", frappe.ValidationError)
    if not frappe.db.sql(
        "SELECT name FROM `tabOperation` WHERE name = %s FOR UPDATE",
        operation_id,
    ):
        frappe.throw("Operation does not exist", frappe.DoesNotExistError)
    operation = frappe.get_doc("Operation", operation_id)
    if not user or user == "Guest" or not (
        "Controller Admin" in frappe.get_roles(user) or operation.requested_by == user
    ):
        frappe.throw("Credential access is not allowed", frappe.PermissionError)
    credential = operation.get_password("administrator_credential", raise_exception=False)
    if not credential or operation.credential_consumed_at:
        frappe.throw("Credential is unavailable or was already consumed", frappe.DoesNotExistError)
    operation.administrator_credential = ""
    operation.credential_consumed_at = now_datetime()
    operation.flags.controller_service = True
    operation.save(ignore_permissions=True)
    return _response({
        "accepted": True,
        "operation_id": operation.operation_id,
        "administrator_credential": credential,
    })
