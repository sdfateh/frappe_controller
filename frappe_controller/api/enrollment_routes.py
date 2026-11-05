"""Frappe HTTP adapters for one-time Agent signing-key enrollment."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import frappe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from werkzeug.wrappers import Response

from ..frappe_security_store import FrappeCertificateStore
from ..feature_flags import agent_environment, runtime_feature_config
from ..controller_settings import load_controller_settings
from ..agent_request_auth import trusted_peer_and_route_from_frappe_request
from ..setup_profiles import required_apps
from ..setup_readiness import installation_checks
from ..security import (
    ControllerRequestError,
    TrustedAdministrator,
    canonical_json,
    parse_exact_json,
)

_TOKEN_FIELDS = frozenset({"agent_id", "ttl_seconds"})
_ENROLLMENT_LIMIT = 96 * 1024
_ENROLL_FIELDS = frozenset({
    "protocol_version", "agent_id", "enrollment_token", "signing_public_key_pem",
})


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


def _json_list(value: Any, field: str) -> list[str]:
    import json

    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        raise ControllerRequestError("bootstrap_policy_invalid", 409) from None
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item or item != item.strip()
        for item in parsed
    ):
        raise ControllerRequestError("bootstrap_policy_invalid", 409)
    return list(dict.fromkeys(parsed))


def _bootstrap_profile(agent_id: str, contract_version: str = "1.0") -> dict[str, Any]:
    settings = load_controller_settings(frappe)
    agent = frappe.db.get_value(
        "Server Agent",
        agent_id,
        ["agent_id", "audience", "protocol_version", "allowed_site_suffixes_json", "allowed_operations_json", "setup_required_apps_json"],
        as_dict=True,
    )
    if not agent:
        raise ControllerRequestError("unknown_or_disabled_agent", 403)
    suffixes = _json_list(agent.allowed_site_suffixes_json, "allowed_site_suffixes_json")
    operations = _json_list(agent.allowed_operations_json, "allowed_operations_json")
    if not settings.public_controller_url or not settings.agent_image_reference or not suffixes or not operations:
        raise ControllerRequestError("bootstrap_policy_incomplete", 409)
    return {
        "contract_version": contract_version,
        "agent": {
            "agent_id": agent.agent_id,
            "audience": agent.audience,
            "protocol_version": agent.protocol_version,
        },
        "controller": {
            "url": settings.public_controller_url,
            "site_name": getattr(getattr(frappe, "local", None), "site", "") or "",
        },
        "policy": {
            "allowed_site_suffixes": suffixes,
            "allowed_operations": operations,
            **({"required_apps": required_apps(agent.setup_required_apps_json)} if contract_version == "1.1" else {}),
        },
        "image": {
            "reference": settings.agent_image_reference,
        },
    }


def _signing_public_key(value: Any) -> str:
    if not isinstance(value, str) or not 64 <= len(value.encode("utf-8")) <= 4096:
        raise ControllerRequestError("agent_signing_key_invalid", 400)
    if "PRIVATE KEY" in value:
        raise ControllerRequestError("private_key_rejected", 400)
    try:
        key = serialization.load_pem_public_key(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise ControllerRequestError("agent_signing_key_invalid", 400) from None
    if not isinstance(key, Ed25519PublicKey):
        raise ControllerRequestError("agent_signing_key_invalid", 400)
    return key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ).hex()


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


@frappe.whitelist(allow_guest=True, methods=["POST"])
def bootstrap_agent_route(**_request_arguments: Any) -> Response:
    """Consume a one-time token, pin a signing key, and return install policy."""
    try:
        raw = _request_body()
        import json
        try:
            candidate = json.loads(raw)
        except (ValueError, TypeError):
            raise ControllerRequestError("invalid_enrollment_request") from None
        fields = _ENROLL_FIELDS | ({"bootstrap_contract_version"} if isinstance(candidate, dict) and "bootstrap_contract_version" in candidate else set())
        body = parse_exact_json(raw, fields, limit=_ENROLLMENT_LIMIT)
        if (
            body.get("protocol_version") != "1.0"
            or not isinstance(body.get("agent_id"), str)
            or not isinstance(body.get("enrollment_token"), str)
        ):
            raise ControllerRequestError("invalid_enrollment_request")
        version = body.get("bootstrap_contract_version", "1.0")
        if version not in ("1.0", "1.1"):
            raise ControllerRequestError("unsupported_bootstrap_contract")
        _require_inventory(body["agent_id"])
        bootstrap = _bootstrap_profile(body["agent_id"], version)
        public_key = _signing_public_key(body.get("signing_public_key_pem"))
        FrappeCertificateStore.from_environment(
            frappe_module=frappe
        ).complete_signing_key_enrollment(
            body["enrollment_token"],
            body["agent_id"],
            public_key,
            actor=f"enrollment-token:{body['agent_id']}",
        )
        return _json_response({"accepted": True, "bootstrap": bootstrap})
    except Exception as error:
        return _error_response(error)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def installation_status_route(**_request_arguments: Any) -> Response:
    """Refresh policy or inspect readiness for the pinned key without leasing work."""
    try:
        body = parse_exact_json(_request_body(), frozenset({"agent_id", "bench_id"}), limit=4096)
        if not isinstance(body["agent_id"], str) or not isinstance(body["bench_id"], str) or len(body["bench_id"]) > 64:
            raise ControllerRequestError("invalid_setup_request")
        peer, routed = trusted_peer_and_route_from_frappe_request(frappe.request, "setup:status")
        FrappeCertificateStore.from_environment(frappe_module=frappe).authenticate_peer(peer, routed, body["agent_id"])
        _require_inventory(body["agent_id"])
        bootstrap = _bootstrap_profile(body["agent_id"], "1.1")
        agent = frappe.db.get_value("Server Agent", body["agent_id"],
            ["enabled", "status", "reported_status", "drain_requested", "last_seen", "inventory_updated_at", "inventory_digest"], as_dict=True)
        bench = frappe.db.get_value("Bench", {"bench_id": body["bench_id"], "server_agent": body["agent_id"]},
            ["server_agent", "enabled", "health_status", "inventory_updated_at", "required_apps_json", "installed_apps_json"], as_dict=True) if body["bench_id"] else None
        # Setup app defaults are a template for new installations. Existing
        # Benches may legitimately have different administrator-owned app sets.
        try:
            bench_apps = required_apps(bench.required_apps_json) if bench and bench.required_apps_json else []
        except (ValueError, TypeError):
            bench_apps = []
        checks = installation_checks(agent, bench, agent_id=body["agent_id"], apps=bench_apps, now=datetime.now(UTC))
        return _json_response({"accepted": True, "bootstrap": bootstrap, "ready": all(checks.values()), "checks": checks, "bench_required_apps": bench_apps})
    except Exception as error:
        return _error_response(error)
