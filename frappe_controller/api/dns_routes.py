"""Fixed Agent-to-Controller routes for controller-owned DNS effects."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Mapping

import frappe
from werkzeug.wrappers import Response

from ..controller_dns import CloudflareDNS, CloudflareDNSConfig, ControllerDNSError
from ..controller_settings import ControllerSettingsError, load_controller_settings
from ..frappe_security_store import FrappeCertificateStore
from ..agent_request_auth import trusted_peer_and_route_from_frappe_request
from ..security import ControllerRequestError, canonical_json, require_agent_id


_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RECORD_ID = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_CREATE_OPERATIONS = frozenset({"site.create", "site.create_blank", "site.create_from_backup"})


def _response(value: Mapping[str, Any], status: int = 200) -> Response:
    return Response(
        canonical_json(value), status=status,
        content_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _body(expected: frozenset[str]) -> dict[str, Any]:
    request = frappe.request
    if request.method != "POST" or request.mimetype != "application/json":
        raise ControllerRequestError("invalid_dns_request", 415)
    raw = request.get_data(cache=True)
    if len(raw) > 4096:
        raise ControllerRequestError("dns_request_too_large", 413)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ControllerRequestError("invalid_dns_request", 400) from None
    if not isinstance(value, dict) or set(value) != expected:
        raise ControllerRequestError("invalid_dns_request", 400)
    return value


def _operation(value: Mapping[str, Any], route_action: str) -> tuple[str, str]:
    agent_id = require_agent_id(value.get("agent_id"))
    operation_id = value.get("operation_id")
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ControllerRequestError("invalid_dns_request", 400)
    peer, routed_agent = trusted_peer_and_route_from_frappe_request(
        frappe.request, route_action
    )
    if peer.agent_id != agent_id or routed_agent != agent_id:
        raise ControllerRequestError("agent_binding_mismatch", 403)
    FrappeCertificateStore.from_environment(
        frappe_module=frappe
    ).authenticate_peer(peer, routed_agent, agent_id)
    row = frappe.db.get_value(
        "Operation", operation_id,
        ["server_agent", "operation_type", "payload_json", "state"], as_dict=True,
    )
    if not row or row.server_agent != agent_id or row.operation_type not in _CREATE_OPERATIONS:
        raise ControllerRequestError("dns_operation_not_owned", 403)
    if row.state not in {"leased", "running"}:
        raise ControllerRequestError("dns_operation_not_active", 409)
    try:
        payload = json.loads(row.payload_json)
        domain = payload["domain"]
    except (TypeError, KeyError, json.JSONDecodeError):
        raise ControllerRequestError("dns_operation_invalid", 409) from None
    if not isinstance(domain, str):
        raise ControllerRequestError("dns_operation_invalid", 409)
    return agent_id, domain


def _dns() -> CloudflareDNS:
    settings = load_controller_settings(frappe)
    return CloudflareDNS(CloudflareDNSConfig(
        api_token=settings.cloudflare_api_token,
        zone_id=settings.cloudflare_zone_id,
        proxied=settings.cloudflare_proxied,
    ))


def _error(error: Exception) -> Response:
    if isinstance(error, ControllerRequestError):
        return _response({"accepted": False, "error": error.code}, error.status)
    if isinstance(error, (ControllerDNSError, ControllerSettingsError)):
        return _response({"accepted": False, "error": "dns_provider_failed"}, 502)
    return _response({"accepted": False, "error": "internal_error"}, 500)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create_record_route(**_request_arguments: Any) -> Response:
    try:
        value = _body(frozenset({"agent_id", "operation_id", "domain"}))
        agent_id, domain = _operation(value, "dns:create")
        if value["domain"] != domain:
            raise ControllerRequestError("dns_domain_mismatch", 403)
        agent = frappe.db.get_value(
            "Server Agent", agent_id, ["enabled", "public_ip"], as_dict=True
        )
        if not agent or not agent.enabled:
            raise ControllerRequestError("unknown_or_disabled_agent", 403)
        try:
            address = str(ipaddress.IPv4Address(agent.public_ip))
        except ipaddress.AddressValueError:
            raise ControllerRequestError("agent_public_ip_invalid", 409) from None
        record_id = _dns().create_a_record(domain, address)
        return _response({"accepted": True, "record_id": record_id})
    except Exception as error:
        frappe.db.rollback()
        return _error(error)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def delete_record_route(**_request_arguments: Any) -> Response:
    try:
        value = _body(frozenset({"agent_id", "operation_id", "record_id"}))
        _agent_id, domain = _operation(value, "dns:delete")
        record_id = value["record_id"]
        if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
            raise ControllerRequestError("invalid_dns_request", 400)
        _dns().delete_owned_record(record_id, domain)
        return _response({"accepted": True, "deleted": True, "record_id": record_id})
    except Exception as error:
        frappe.db.rollback()
        return _error(error)
