"""Direct Ed25519 authentication for Agent requests over normal HTTPS."""

from __future__ import annotations

import base64
import hashlib
import re
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .security import (
    NETWORK_ISOLATED_FINGERPRINT,
    NETWORK_ISOLATED_SERIAL,
    ControllerRequestError,
    TrustedPeerIdentity,
)

_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEQUENCE = re.compile(r"^[1-9][0-9]{0,19}$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_MAX_CLOCK_AGE_NS = 300 * 1_000_000_000
_MAX_FUTURE_NS = 30 * 1_000_000_000
_MAX_BODY = 1024 * 1024


def _canonical(agent_id: str, action: str, sequence: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"frappe-agent-v1\n{agent_id}\n{action}\n{sequence}\n{digest}".encode("ascii")


def trusted_peer_and_route_from_frappe_request(
    request: Any, expected_action: str, database: Any | None = None,
) -> tuple[TrustedPeerIdentity, str]:
    """Verify a body-bound signature and durably reject replayed sequences."""
    if database is None:
        import frappe

        database = frappe.db
    if not isinstance(expected_action, str) or not expected_action:
        raise ControllerRequestError("fixed_route_mismatch", 404)
    headers = getattr(request, "headers", None)
    if headers is None:
        raise ControllerRequestError("agent_signature_missing", 401)
    agent_id = headers.get("X-Frappe-Agent-ID", "")
    sequence = headers.get("X-Frappe-Agent-Sequence", "")
    signature_text = headers.get("X-Frappe-Agent-Signature", "")
    if (
        not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id)
        or not isinstance(sequence, str) or not _SEQUENCE.fullmatch(sequence)
        or not isinstance(signature_text, str) or not _SIGNATURE.fullmatch(signature_text)
    ):
        raise ControllerRequestError("agent_signature_invalid", 401)
    sequence_value = int(sequence)
    current = time.time_ns()
    if (
        sequence_value < current - _MAX_CLOCK_AGE_NS
        or sequence_value > current + _MAX_FUTURE_NS
    ):
        raise ControllerRequestError("agent_signature_expired", 401)
    raw = request.get_data(cache=True)
    if not isinstance(raw, bytes) or len(raw) > _MAX_BODY:
        raise ControllerRequestError("request_too_large", 413)
    rows = database.sql(
        "SELECT name,enabled,signing_public_key_ed25519,last_signed_request_at_ns "
        "FROM `tabServer Agent` WHERE agent_id=%s LIMIT 1 FOR UPDATE",
        (agent_id,), as_dict=True,
    )
    if not rows or not rows[0]["enabled"]:
        raise ControllerRequestError("unknown_or_disabled_agent", 403)
    public_text = rows[0].get("signing_public_key_ed25519") or ""
    if not re.fullmatch(r"[0-9a-f]{64}", public_text):
        raise ControllerRequestError("agent_signing_key_missing", 401)
    try:
        signature = base64.urlsafe_b64decode(signature_text + "==")
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_text))
        public_key.verify(signature, _canonical(agent_id, expected_action, sequence, raw))
    except (ValueError, InvalidSignature):
        raise ControllerRequestError("agent_signature_invalid", 401) from None
    previous = rows[0].get("last_signed_request_at_ns") or "0"
    if (
        not isinstance(previous, str)
        or not previous.isdigit()
        or sequence_value <= int(previous)
    ):
        raise ControllerRequestError("agent_request_replayed", 409)
    database.set_value(
        "Server Agent", rows[0]["name"], "last_signed_request_at_ns", sequence,
        update_modified=False,
    )
    database.commit()
    return TrustedPeerIdentity(
        agent_id=agent_id,
        certificate_serial=NETWORK_ISOLATED_SERIAL,
        certificate_fingerprint_sha256=NETWORK_ISOLATED_FINGERPRINT,
        verification="ed25519_signature",
    ), agent_id


__all__ = ["trusted_peer_and_route_from_frappe_request"]
