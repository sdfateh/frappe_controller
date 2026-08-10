"""Agent identity for fixed Frappe agent routes.

Deliberately unauthenticated: the controller is reachable exclusively by
container hostname on an isolated, non-public Docker network with no other
tenants, so agent_id is trusted directly from the request body with no proxy
token, no client-verify header, and no certificate at all.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .security import (
    NETWORK_ISOLATED_FINGERPRINT,
    NETWORK_ISOLATED_SERIAL,
    ControllerRequestError,
    TrustedPeerIdentity,
    require_agent_id,
)


def _peek_agent_id_from_body(request: Any) -> str:
    """Read agent_id directly from the JSON body with no other trust signal.

    api/agent.py's normal parse_exact_json still separately validates the
    full body afterward; this only extracts identity ahead of that.
    """
    get_data = getattr(request, "get_data", None)
    if get_data is None:
        raise ControllerRequestError("trusted_proxy_context_missing", 401)
    try:
        payload = json.loads(get_data(cache=True).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ControllerRequestError("invalid_json") from None
    if not isinstance(payload, Mapping):
        raise ControllerRequestError("invalid_json")
    try:
        return require_agent_id(payload.get("agent_id"))
    except ValueError:
        raise ControllerRequestError("invalid_agent", 404) from None


def trusted_peer_and_route_from_frappe_request(
    request: Any, expected_action: str
) -> tuple[TrustedPeerIdentity, str]:
    """Trust whatever agent_id the request body claims, with no other check."""
    del expected_action  # no proxy-captured route to bind against
    agent_id = _peek_agent_id_from_body(request)
    peer = TrustedPeerIdentity(
        agent_id=agent_id,
        certificate_serial=NETWORK_ISOLATED_SERIAL,
        certificate_fingerprint_sha256=NETWORK_ISOLATED_FINGERPRINT,
        verification="network_isolated_trust",
    )
    return peer, agent_id
