"""Trusted TLS-boundary adapters for fixed Frappe agent routes.

Identity headers are useful only after the direct application peer and a
protected proxy token have both been authenticated.  Agent identity is never
read from a header: it is derived from the CA-verified leaf certificate SAN.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote_to_bytes

from cryptography.hazmat.primitives import hashes

from .pki import validate_agent_certificate
from .security import ControllerRequestError, TrustedPeerIdentity

PROXY_TOKEN_ENVIRON_KEY = "HTTP_X_FRAPPE_PROXY_TOKEN"
CLIENT_VERIFY_ENVIRON_KEY = "HTTP_X_FRAPPE_CLIENT_VERIFY"
CLIENT_CERT_ENVIRON_KEY = "HTTP_X_FRAPPE_CLIENT_CERT"
ORIGINAL_ROUTE_ENVIRON_KEY = "HTTP_X_FRAPPE_ORIGINAL_ROUTE"
_ROUTE = re.compile(
    r"^/v1/agents/(?P<agent_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})/"
    r"(?P<action>protocol:negotiate|heartbeat|commands:poll|events|results|certificates:rotate)$"
)
_PROXY_TOKEN = re.compile(rb"^[A-Za-z0-9_-]{32,256}$")


@dataclass(frozen=True, slots=True)
class TrustedProxyPolicy:
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    token: bytes
    agent_ca_pem: bytes

    def __post_init__(self) -> None:
        if not self.networks:
            raise ValueError("at least one trusted proxy network is required")
        if not isinstance(self.token, bytes) or not _PROXY_TOKEN.fullmatch(self.token):
            raise ValueError("trusted proxy token must be 32 to 256 URL-safe characters")
        if not isinstance(self.agent_ca_pem, bytes) or b"BEGIN CERTIFICATE" not in self.agent_ca_pem:
            raise ValueError("agent CA certificate is required")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "TrustedProxyPolicy":
        values = environ if environ is not None else os.environ
        raw_networks = values.get("FRAPPE_CONTROLLER_TRUSTED_PROXY_CIDRS", "")
        token_path = values.get("FRAPPE_CONTROLLER_PROXY_TOKEN_FILE", "")
        ca_path = values.get("FRAPPE_CONTROLLER_AGENT_CA_FILE", "")
        if not raw_networks or not token_path or not ca_path:
            raise RuntimeError("trusted proxy CIDRs, token file, and agent CA file are required")
        try:
            networks = tuple(
                ipaddress.ip_network(item.strip(), strict=True)
                for item in raw_networks.split(",")
                if item.strip()
            )
        except ValueError:
            raise RuntimeError("trusted proxy CIDR configuration is invalid") from None
        token = Path(token_path).read_bytes().rstrip(b"\r\n")
        ca_pem = Path(ca_path).read_bytes()
        return cls(networks=networks, token=token, agent_ca_pem=ca_pem)


def _request_environ(request: Any) -> Mapping[str, Any]:
    environ = getattr(request, "environ", None)
    if not isinstance(environ, Mapping):
        raise ControllerRequestError("trusted_proxy_context_missing", 401)
    return environ


def _authorize_proxy(environ: Mapping[str, Any], policy: TrustedProxyPolicy) -> None:
    remote_address = environ.get("REMOTE_ADDR")
    presented_token = environ.get(PROXY_TOKEN_ENVIRON_KEY)
    if not isinstance(remote_address, str) or not isinstance(presented_token, str):
        raise ControllerRequestError("untrusted_proxy", 401)
    try:
        address = ipaddress.ip_address(remote_address)
    except ValueError:
        raise ControllerRequestError("untrusted_proxy", 401) from None
    if not any(address.version == network.version and address in network for network in policy.networks):
        raise ControllerRequestError("untrusted_proxy", 401)
    try:
        token_bytes = presented_token.encode("ascii", "strict")
    except UnicodeEncodeError:
        raise ControllerRequestError("untrusted_proxy", 401) from None
    if not hmac.compare_digest(token_bytes, policy.token):
        raise ControllerRequestError("untrusted_proxy", 401)


def trusted_peer_from_certificate(
    certificate_pem: bytes | str,
    agent_ca_pem: bytes | str,
    *,
    verification: str = "direct_mtls",
) -> TrustedPeerIdentity:
    """Create a peer identity from server-owned direct TLS certificate state."""
    certificate, agent_id = validate_agent_certificate(certificate_pem, agent_ca_pem)
    return TrustedPeerIdentity(
        agent_id=agent_id,
        certificate_serial=format(certificate.serial_number, "X"),
        certificate_fingerprint_sha256=certificate.fingerprint(hashes.SHA256()).hex(),
        verification=verification,
    )


def trusted_peer_from_frappe_request(
    request: Any,
    policy: TrustedProxyPolicy | None = None,
) -> TrustedPeerIdentity:
    """Derive a certificate identity from an authenticated pinned proxy hop."""
    policy = policy or TrustedProxyPolicy.from_environment()
    environ = _request_environ(request)
    _authorize_proxy(environ, policy)
    if environ.get(CLIENT_VERIFY_ENVIRON_KEY) != "SUCCESS":
        raise ControllerRequestError("client_certificate_not_verified", 401)
    escaped_certificate = environ.get(CLIENT_CERT_ENVIRON_KEY)
    if not isinstance(escaped_certificate, str) or len(escaped_certificate) > 96 * 1024:
        raise ControllerRequestError("client_certificate_invalid", 401)
    try:
        certificate_pem = unquote_to_bytes(escaped_certificate)
    except (UnicodeEncodeError, ValueError):
        raise ControllerRequestError("client_certificate_invalid", 401) from None
    return trusted_peer_from_certificate(
        certificate_pem,
        policy.agent_ca_pem,
        verification="pinned_proxy_mtls",
    )


def trusted_peer_and_agent_from_frappe_request(
    request: Any,
    policy: TrustedProxyPolicy | None = None,
) -> tuple[TrustedPeerIdentity, str]:
    peer = trusted_peer_from_frappe_request(request, policy)
    return peer, peer.agent_id


def trusted_route_from_frappe_request(
    request: Any,
    expected_action: str,
    policy: TrustedProxyPolicy | None = None,
) -> str:
    """Return the proxy-captured path agent, never a Frappe method argument."""
    policy = policy or TrustedProxyPolicy.from_environment()
    environ = _request_environ(request)
    _authorize_proxy(environ, policy)
    original_route = environ.get(ORIGINAL_ROUTE_ENVIRON_KEY)
    if not isinstance(original_route, str):
        raise ControllerRequestError("fixed_route_missing", 404)
    match = _ROUTE.fullmatch(original_route)
    if match is None or not hmac.compare_digest(match.group("action"), expected_action):
        raise ControllerRequestError("fixed_route_mismatch", 404)
    return match.group("agent_id")


def trusted_peer_and_route_from_frappe_request(
    request: Any,
    expected_action: str,
    policy: TrustedProxyPolicy | None = None,
) -> tuple[TrustedPeerIdentity, str]:
    """Authenticate one request and bind its client SAN to its captured path."""
    policy = policy or TrustedProxyPolicy.from_environment()
    peer = trusted_peer_from_frappe_request(request, policy)
    path_agent_id = trusted_route_from_frappe_request(request, expected_action, policy)
    if not hmac.compare_digest(peer.agent_id, path_agent_id):
        raise ControllerRequestError("agent_binding_mismatch", 403)
    return peer, path_agent_id
