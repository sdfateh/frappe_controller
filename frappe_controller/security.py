"""Security boundary types and strict JSON helpers for controller routes."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

PROTOCOL_VERSION = "1.0"
AUDIENCE = "frappe-controller"
MAX_REQUEST_BYTES = 1024 * 1024
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SERIAL = re.compile(r"^[A-Fa-f0-9]{1,64}$")
_FORBIDDEN_KEYS = frozenset({
    "api_key", "api_secret", "authorization", "client_secret", "credential",
    "db_password", "password", "private_key", "secret", "token",
})
_ROUTING_KEYS = frozenset({
    "compose_file", "backend_service", "sites_path", "host_staging_path",
    "container_staging_path", "db_secret_ref", "command", "shell", "sql",
})


# Sentinel certificate fields for TrustedPeerIdentity(verification="network_isolated_trust").
# Never valid values for a genuinely verified certificate.
NETWORK_ISOLATED_SERIAL = "0"
NETWORK_ISOLATED_FINGERPRINT = "0" * 64


class ControllerRequestError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TrustedAdministrator:
    """Audit identity produced by an authenticated enrollment adapter."""

    actor_id: str
    authentication: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id:
            raise ValueError("administrator actor is invalid")
        if self.authentication not in {"frappe_session", "service_token", "one_time_token"}:
            raise ValueError("administrator authentication context is untrusted")


@dataclass(frozen=True, slots=True)
class TrustedPeerIdentity:
    """Agent identity established by a verified request signature.

    Signed requests use fixed sentinel values in the legacy certificate
    fields. The private signing key remains only on the managed server.
    """

    agent_id: str
    certificate_serial: str
    certificate_fingerprint_sha256: str
    verification: str

    def __post_init__(self) -> None:
        require_agent_id(self.agent_id)
        if not _SERIAL.fullmatch(self.certificate_serial):
            raise ValueError("certificate serial is invalid")
        if not _HEX64.fullmatch(self.certificate_fingerprint_sha256):
            raise ValueError("certificate fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class VerifiedCSR:
    agent_id: str
    public_key_sha256: str
    csr_sha256: str
    # The DER is retained only on this short-lived boundary value so that a CA
    # can issue for the exact key and signed request that the verifier checked.
    # Repositories persist the three fields above and never this payload.
    csr_der: bytes = field(default=b"", repr=False, compare=False)

    def __post_init__(self) -> None:
        require_agent_id(self.agent_id)
        if not _HEX64.fullmatch(self.public_key_sha256) or not _HEX64.fullmatch(self.csr_sha256):
            raise ValueError("verified CSR digests are invalid")
        if not isinstance(self.csr_der, bytes) or len(self.csr_der) > 64 * 1024:
            raise ValueError("verified CSR payload is invalid")
        if self.csr_der and not hashlib.sha256(self.csr_der).hexdigest() == self.csr_sha256:
            raise ValueError("verified CSR digest does not match its payload")


@dataclass(frozen=True, slots=True)
class CertificateIssuance:
    serial: str
    fingerprint_sha256: str
    public_key_sha256: str
    not_before: datetime
    not_after: datetime
    certificate_pem: str
    ca_chain_pem: str

    def __post_init__(self) -> None:
        if not _SERIAL.fullmatch(self.serial):
            raise ValueError("issued certificate serial is invalid")
        if not _HEX64.fullmatch(self.fingerprint_sha256) or not _HEX64.fullmatch(self.public_key_sha256):
            raise ValueError("issued certificate digest is invalid")
        if utc(self.not_after) <= utc(self.not_before):
            raise ValueError("issued certificate validity is invalid")
        if "PRIVATE KEY" in self.certificate_pem or "PRIVATE KEY" in self.ca_chain_pem:
            raise ValueError("certificate response must never contain a private key")
        if "BEGIN CERTIFICATE" not in self.certificate_pem:
            raise ValueError("issued certificate PEM is invalid")


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def timestamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def require_agent_id(value: str) -> str:
    if not isinstance(value, str) or not _AGENT_ID.fullmatch(value):
        raise ValueError("agent_id is invalid")
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControllerRequestError("duplicate_json_key")
        result[key] = value
    return result


def parse_exact_json(
    raw: bytes | str | Mapping[str, Any],
    required_fields: frozenset[str],
    *,
    limit: int = MAX_REQUEST_BYTES,
) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if len(raw) > limit:
            raise ControllerRequestError("request_too_large", 413)
        try:
            decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControllerRequestError("invalid_json") from None
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > limit:
            raise ControllerRequestError("request_too_large", 413)
        try:
            decoded = json.loads(raw, object_pairs_hook=_strict_object)
        except json.JSONDecodeError:
            raise ControllerRequestError("invalid_json") from None
    elif isinstance(raw, Mapping):
        decoded = dict(raw)
    else:
        raise ControllerRequestError("invalid_json")
    if not isinstance(decoded, dict) or frozenset(decoded) != required_fields:
        raise ControllerRequestError("invalid_fields")
    validate_json(decoded)
    if len(canonical_json(decoded).encode("utf-8")) > limit:
        raise ControllerRequestError("request_too_large", 413)
    return decoded


def validate_json(value: Any, *, reject_sensitive: bool = False, reject_routing: bool = False) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ControllerRequestError("invalid_json")
            normalized = key.casefold().replace("-", "_")
            if reject_sensitive and any(marker in normalized for marker in _FORBIDDEN_KEYS):
                raise ControllerRequestError("secret_field_rejected")
            if reject_routing and normalized in _ROUTING_KEYS:
                raise ControllerRequestError("routing_field_rejected")
            validate_json(child, reject_sensitive=reject_sensitive, reject_routing=reject_routing)
    elif isinstance(value, list):
        for child in value:
            validate_json(child, reject_sensitive=reject_sensitive, reject_routing=reject_routing)
    elif value is None or isinstance(value, (str, bool, int)):
        return
    elif isinstance(value, float) and math.isfinite(value):
        return
    else:
        raise ControllerRequestError("invalid_json")


def canonical_json(value: Any) -> str:
    validate_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_common(body: Mapping[str, Any], path_agent_id: str) -> str:
    try:
        path_agent_id = require_agent_id(path_agent_id)
    except ValueError:
        raise ControllerRequestError("invalid_agent", 404) from None
    if body.get("protocol_version") != PROTOCOL_VERSION:
        raise ControllerRequestError("unsupported_protocol")
    if body.get("audience") != AUDIENCE:
        raise ControllerRequestError("wrong_audience", 403)
    if body.get("agent_id") != path_agent_id:
        raise ControllerRequestError("agent_binding_mismatch", 403)
    return path_agent_id
