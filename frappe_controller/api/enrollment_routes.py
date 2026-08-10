"""Frappe HTTP adapters for enrollment tokens, issuance, and rotation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from werkzeug.wrappers import Response

from ..frappe_security_store import FrappeCertificateStore
from ..feature_flags import agent_environment, runtime_feature_config
from ..proxy_security import trusted_peer_and_route_from_frappe_request
from ..security import (
    ControllerRequestError,
    TrustedAdministrator,
    canonical_json,
    parse_exact_json,
)
from .enrollment import FrappeEnrollmentRuntime, enroll_agent, rotate_certificate

_TOKEN_FIELDS = frozenset({"agent_id", "ttl_seconds"})
_ENROLLMENT_LIMIT = 96 * 1024
_ENROLL_FIELDS = frozenset({"protocol_version", "agent_id", "enrollment_token", "csr_pem"})


def _json_response(payload: Mapping[str, Any], *, status: int = 200) -> Response:
    return Response(
        canonical_json(payload),
        status=status,
        content_type="application/json; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _request_body() -> bytes:
    request = frappe.request
    if request.method != "POST":
        raise ControllerRequestError("method_not_allowed", 405)
    if request.mimetype != "application/json":
        raise ControllerRequestError("content_type_required", 415)
    if request.content_length is not None and request.content_length > _ENROLLMENT_LIMIT:
        raise ControllerRequestError("request_too_large", 413)
    raw = request.get_data(cache=True)
    if len(raw) > _ENROLLMENT_LIMIT:
        raise ControllerRequestError("request_too_large", 413)
    return raw


def _administrator() -> TrustedAdministrator:
    user = getattr(getattr(frappe, "session", None), "user", None)
    if not isinstance(user, str) or not user or user == "Guest":
        raise ControllerRequestError("administrator_authentication_required", 401)
    if "Controller Admin" not in set(frappe.get_roles(user)):
        raise ControllerRequestError("administrator_authorization_required", 403)
    return TrustedAdministrator(user, "frappe_session")


def _require_inventory(agent_id: str) -> None:
    environment = agent_environment(frappe, agent_id)
    if environment is None or not runtime_feature_config(frappe).enabled(
        "inventory", environment
    ):
        raise ControllerRequestError("feature_disabled", 403)


def _error_response(error: Exception) -> Response:
    frappe.db.rollback()
    if isinstance(error, ControllerRequestError):
        return _json_response({"accepted": False, "error": error.code}, status=error.status)
    return _json_response({"accepted": False, "error": "internal_error"}, status=500)


@frappe.whitelist(methods=["POST"])
def create_enrollment_token_route(**_request_arguments: Any) -> Response:
    """Create a one-time token; its plaintext is returned exactly once."""
    try:
        administrator = _administrator()
        body = parse_exact_json(_request_body(), _TOKEN_FIELDS, limit=_ENROLLMENT_LIMIT)
        if not isinstance(body["agent_id"], str) or type(body["ttl_seconds"]) is not int:
            raise ControllerRequestError("invalid_enrollment_request")
        _require_inventory(body["agent_id"])
        store = FrappeCertificateStore.from_environment(frappe_module=frappe)
        token = store.create_enrollment_token(
            body["agent_id"],
            actor=administrator.actor_id,
            ttl_seconds=body["ttl_seconds"],
        )
        return _json_response({"accepted": True, "enrollment_token": token})
    except Exception as error:
        return _error_response(error)


@frappe.whitelist(methods=["POST"])
def enroll_agent_route(**_request_arguments: Any) -> Response:
    """Issue for an administrator-reviewed CSR using a one-time bound token."""
    try:
        administrator = _administrator()
        raw = _request_body()
        body = parse_exact_json(raw, _ENROLL_FIELDS, limit=_ENROLLMENT_LIMIT)
        if not isinstance(body["agent_id"], str):
            raise ControllerRequestError("invalid_enrollment_request")
        _require_inventory(body["agent_id"])
        runtime = FrappeEnrollmentRuntime.from_environment(frappe_module=frappe)
        response = enroll_agent(
            runtime.store,
            raw,
            administrator=administrator,
            verify_csr=runtime.verify_csr,
            issue_certificate=runtime.issue_certificate,
        )
        return _json_response(response)
    except Exception as error:
        return _error_response(error)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def rotate_certificate_route(**_request_arguments: Any) -> Response:
    """Rotate only for the certificate-bound fixed agent route."""
    try:
        peer, path_agent_id = trusted_peer_and_route_from_frappe_request(
            frappe.request, "certificates:rotate"
        )
        raw = _request_body()
        overlap = frappe.conf.get("frappe_controller_certificate_overlap_seconds", 120)
        if type(overlap) is not int or not 0 <= overlap <= 300:
            raise RuntimeError("invalid certificate overlap configuration")
        runtime = FrappeEnrollmentRuntime.from_environment(frappe_module=frappe)
        response = rotate_certificate(
            runtime.store,
            path_agent_id,
            raw,
            peer,
            verify_csr=runtime.verify_csr,
            issue_certificate=runtime.issue_certificate,
            overlap_seconds=overlap,
        )
        return _json_response(response)
    except Exception as error:
        return _error_response(error)
