"""Transactional Frappe persistence for enrollment and certificate lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .security import (
    CertificateIssuance,
    ControllerRequestError,
    TrustedPeerIdentity,
    VerifiedCSR,
    require_agent_id,
    utc,
)


def _runtime() -> Any:
    import frappe

    return frappe


def _current(now: datetime | None) -> datetime:
    return utc(now or datetime.now(UTC))


def _database_time(value: datetime) -> datetime:
    """Frappe/MariaDB stores naive values; controller storage is always UTC."""
    return utc(value).replace(tzinfo=None)


def _from_database(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise ControllerRequestError("certificate_record_invalid", 500)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class FrappeCertificateStore:
    """ControllerStore-compatible security subset backed by Frappe DocTypes."""

    def __init__(self, *, token_pepper: bytes, frappe_module: Any | None = None) -> None:
        if not isinstance(token_pepper, bytes) or len(token_pepper) < 32:
            raise ValueError("token pepper must contain at least 32 bytes")
        self.frappe = frappe_module or _runtime()
        self.db = self.frappe.db
        self._pepper = token_pepper

    @classmethod
    def from_environment(
        cls,
        *,
        environ: dict[str, str] | None = None,
        frappe_module: Any | None = None,
    ) -> "FrappeCertificateStore":
        values = environ if environ is not None else os.environ
        pepper_path = values.get("FRAPPE_CONTROLLER_TOKEN_PEPPER_FILE", "")
        if not pepper_path:
            raise RuntimeError("FRAPPE_CONTROLLER_TOKEN_PEPPER_FILE is required")
        pepper = Path(pepper_path).read_bytes().rstrip(b"\r\n")
        return cls(token_pepper=pepper, frappe_module=frappe_module)

    def _token_hash(self, raw_token: str) -> str:
        if not isinstance(raw_token, str) or not raw_token:
            raise ControllerRequestError("invalid_enrollment_token", 403)
        return hmac.new(self._pepper, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _lock_agent(self, agent_id: str) -> dict[str, Any]:
        rows = self.db.sql(
            "SELECT name,agent_id,enabled,expected_public_key_sha256,"
            "signing_public_key_ed25519,"
            "enrollment_token_hash,enrollment_expires_at,enrollment_consumed_at,"
            "enrollment_completed_at FROM `tabServer Agent` "
            "WHERE agent_id=%s LIMIT 1 FOR UPDATE",
            (agent_id,),
            as_dict=True,
        )
        if not rows:
            raise ControllerRequestError("unknown_or_disabled_agent", 403)
        return dict(rows[0])

    def create_enrollment_token(
        self,
        agent_id: str,
        *,
        actor: str,
        ttl_seconds: int = 600,
        now: datetime | None = None,
    ) -> str:
        require_agent_id(agent_id)
        if not isinstance(actor, str) or not actor or not 1 <= ttl_seconds <= 3600:
            raise ValueError("enrollment token policy is invalid")
        issued = _current(now)
        agent = self._lock_agent(agent_id)
        raw_token = secrets.token_urlsafe(32)
        self.db.set_value(
            "Server Agent",
            agent["name"],
            {
                "enrollment_token_hash": self._token_hash(raw_token),
                "enrollment_expires_at": _database_time(issued + timedelta(seconds=ttl_seconds)),
                "enrollment_token_created_at": _database_time(issued),
                "enrollment_token_created_by": actor,
                "enrollment_consumed_at": None,
                "enrollment_completed_at": None,
            },
            update_modified=False,
        )
        return raw_token

    def complete_signing_key_enrollment(
        self,
        raw_token: str,
        agent_id: str,
        signing_public_key_ed25519: str,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> None:
        """Atomically consume a one-time token and pin an Ed25519 public key."""
        require_agent_id(agent_id)
        if (
            not isinstance(signing_public_key_ed25519, str)
            or len(signing_public_key_ed25519) != 64
        ):
            raise ControllerRequestError("agent_signing_key_invalid", 400)
        try:
            public_bytes = bytes.fromhex(signing_public_key_ed25519)
        except ValueError:
            raise ControllerRequestError("agent_signing_key_invalid", 400) from None
        current = _current(now)
        agent = self._lock_agent(agent_id)
        if not hmac.compare_digest(
            agent.get("enrollment_token_hash") or "", self._token_hash(raw_token)
        ):
            raise ControllerRequestError("invalid_enrollment_token", 403)
        expires_at = agent.get("enrollment_expires_at")
        if expires_at is None or _from_database(expires_at) <= current:
            raise ControllerRequestError("enrollment_token_expired", 403)
        if agent.get("enrollment_consumed_at") is not None:
            if (
                agent.get("enrollment_completed_at") is not None
                and hmac.compare_digest(
                    agent.get("signing_public_key_ed25519") or "",
                    signing_public_key_ed25519,
                )
            ):
                return
            raise ControllerRequestError("enrollment_token_used", 409)
        key_digest = hashlib.sha256(public_bytes).hexdigest()
        self.db.set_value(
            "Server Agent",
            agent["name"],
            {
                "expected_public_key_sha256": key_digest,
                "signing_public_key_ed25519": signing_public_key_ed25519,
                "last_signed_request_at_ns": None,
                "enrollment_consumed_at": _database_time(current),
                "enrollment_completed_at": _database_time(current),
                "enabled": 1,
                "status": "Offline",
            },
            update_modified=False,
        )
        self.db.commit()

    def claim_enrollment_token(
        self,
        raw_token: str,
        verified: VerifiedCSR,
        *,
        now: datetime | None = None,
    ) -> None:
        current = _current(now)
        agent = self._lock_agent(verified.agent_id)
        stored_hash = agent.get("enrollment_token_hash") or ""
        if not hmac.compare_digest(stored_hash, self._token_hash(raw_token)):
            raise ControllerRequestError("invalid_enrollment_token", 403)
        if agent.get("enrollment_consumed_at") is not None:
            raise ControllerRequestError("enrollment_token_used", 409)
        expires_at = agent.get("enrollment_expires_at")
        if expires_at is None or _from_database(expires_at) <= current:
            raise ControllerRequestError("enrollment_token_expired", 403)
        expected_key = agent.get("expected_public_key_sha256") or ""
        if expected_key and not hmac.compare_digest(expected_key, verified.public_key_sha256):
            raise ControllerRequestError("csr_key_mismatch", 403)
        if not expected_key:
            # The one-time token authorizes the first key. Pin it atomically so
            # every retry, completion, rotation, and future token stays bound.
            self.db.set_value(
                "Server Agent",
                agent["name"],
                "expected_public_key_sha256",
                verified.public_key_sha256,
                update_modified=False,
            )
        self.db.set_value(
            "Server Agent",
            agent["name"],
            "enrollment_consumed_at",
            _database_time(current),
            update_modified=False,
        )
        # Claiming is deliberately durable before CA work.  An issuer failure
        # burns the token instead of allowing an attacker to replay it.
        self.db.commit()

    def complete_enrollment(
        self,
        raw_token: str,
        verified: VerifiedCSR,
        issuance: CertificateIssuance,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> None:
        current = _current(now)
        if not hmac.compare_digest(issuance.public_key_sha256, verified.public_key_sha256):
            raise ControllerRequestError("certificate_key_mismatch", 409)
        agent = self._lock_agent(verified.agent_id)
        if (
            not hmac.compare_digest(agent.get("enrollment_token_hash") or "", self._token_hash(raw_token))
            or agent.get("enrollment_consumed_at") is None
            or agent.get("enrollment_completed_at") is not None
        ):
            raise ControllerRequestError("enrollment_completion_conflict", 409)
        if not hmac.compare_digest(
            agent.get("expected_public_key_sha256") or verified.public_key_sha256,
            verified.public_key_sha256,
        ):
            raise ControllerRequestError("csr_key_mismatch", 403)
        self._insert_certificate(agent["name"], issuance, actor=actor, now=current)
        self.db.set_value(
            "Server Agent",
            agent["name"],
            {
                "enrollment_completed_at": _database_time(current),
                "enabled": 1,
                "status": "Offline",
            },
            update_modified=False,
        )

    def _insert_certificate(
        self,
        server_agent: str,
        issuance: CertificateIssuance,
        *,
        actor: str,
        now: datetime,
    ) -> None:
        if not (utc(issuance.not_before) <= now < utc(issuance.not_after)):
            raise ControllerRequestError("certificate_not_current", 409)
        document = self.frappe.get_doc({
            "doctype": "Agent Certificate",
            "server_agent": server_agent,
            "serial_number": issuance.serial,
            "fingerprint_sha256": issuance.fingerprint_sha256,
            "public_key_sha256": issuance.public_key_sha256,
            "status": "Active",
            "valid_from": _database_time(issuance.not_before),
            "valid_until": _database_time(issuance.not_after),
            "issued_at": _database_time(now),
            "issued_by": actor,
        })
        duplicate_error = getattr(self.frappe, "DuplicateEntryError", ())
        try:
            document.insert(ignore_permissions=True)
        except duplicate_error:
            raise ControllerRequestError("certificate_identity_conflict", 409) from None

    def authenticate_peer(
        self,
        peer: TrustedPeerIdentity,
        path_agent_id: str,
        body_agent_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        if type(peer) is not TrustedPeerIdentity:
            raise ControllerRequestError("untrusted_peer_identity", 401)
        if peer.agent_id != path_agent_id or body_agent_id != path_agent_id:
            raise ControllerRequestError("agent_binding_mismatch", 403)
        if peer.verification == "ed25519_signature":
            rows = self.db.sql(
                "SELECT enabled,signing_public_key_ed25519 FROM `tabServer Agent` "
                "WHERE agent_id=%s LIMIT 1",
                (path_agent_id,),
                as_dict=True,
            )
            if (
                not rows or not rows[0]["enabled"]
                or not rows[0].get("signing_public_key_ed25519")
            ):
                raise ControllerRequestError("agent_signature_not_authorized", 401)
            return
        current = _current(now)
        rows = self.db.sql(
            "SELECT c.name,c.fingerprint_sha256,c.status,c.valid_from,c.valid_until,"
            "c.overlap_until,a.enabled FROM `tabAgent Certificate` c "
            "JOIN `tabServer Agent` a ON a.name=c.server_agent "
            "WHERE a.agent_id=%s AND c.serial_number=%s LIMIT 1",
            (path_agent_id, peer.certificate_serial),
            as_dict=True,
        )
        if not rows:
            raise ControllerRequestError("certificate_not_authorized", 401)
        certificate = rows[0]
        if not certificate["enabled"] or not hmac.compare_digest(
            certificate["fingerprint_sha256"], peer.certificate_fingerprint_sha256
        ):
            raise ControllerRequestError("certificate_not_authorized", 401)
        status_valid = certificate["status"] == "Active" or (
            certificate["status"] == "Rotating"
            and certificate.get("overlap_until") is not None
            and _from_database(certificate["overlap_until"]) > current
        )
        if not status_valid or not (
            _from_database(certificate["valid_from"])
            <= current
            < _from_database(certificate["valid_until"])
        ):
            raise ControllerRequestError("certificate_not_authorized", 401)

    def rotate_certificate(
        self,
        peer: TrustedPeerIdentity,
        verified: VerifiedCSR,
        issuance: CertificateIssuance,
        *,
        actor: str,
        overlap_seconds: int = 120,
        now: datetime | None = None,
    ) -> None:
        if not 0 <= overlap_seconds <= 300:
            raise ValueError("certificate overlap exceeds policy")
        current = _current(now)
        self.authenticate_peer(peer, verified.agent_id, verified.agent_id, now=current)
        if not hmac.compare_digest(issuance.public_key_sha256, verified.public_key_sha256):
            raise ControllerRequestError("certificate_key_mismatch", 409)
        agents = self.db.sql(
            "SELECT name FROM `tabServer Agent` WHERE agent_id=%s AND enabled=1 "
            "LIMIT 1 FOR UPDATE",
            (verified.agent_id,),
            as_dict=True,
        )
        certificates = self.db.sql(
            "SELECT c.name,c.status FROM `tabAgent Certificate` c "
            "JOIN `tabServer Agent` a ON a.name=c.server_agent "
            "WHERE a.agent_id=%s AND c.serial_number=%s LIMIT 1 FOR UPDATE",
            (verified.agent_id, peer.certificate_serial),
            as_dict=True,
        )
        if not agents or not certificates or certificates[0]["status"] != "Active":
            raise ControllerRequestError("certificate_rotation_conflict", 409)
        self.db.set_value(
            "Agent Certificate",
            certificates[0]["name"],
            {
                "status": "Rotating",
                "overlap_until": _database_time(current + timedelta(seconds=overlap_seconds)),
            },
            update_modified=False,
        )
        self._insert_certificate(agents[0]["name"], issuance, actor=actor, now=current)

    def revoke_certificate(
        self,
        agent_id: str,
        serial: str,
        *,
        actor: str,
        reason: str = "administrative revocation",
        now: datetime | None = None,
    ) -> None:
        rows = self.db.sql(
            "SELECT c.name,c.status FROM `tabAgent Certificate` c "
            "JOIN `tabServer Agent` a ON a.name=c.server_agent "
            "WHERE a.agent_id=%s AND c.serial_number=%s LIMIT 1 FOR UPDATE",
            (agent_id, serial),
            as_dict=True,
        )
        if not rows or rows[0]["status"] == "Revoked":
            raise ControllerRequestError("certificate_not_found", 404)
        self.db.set_value(
            "Agent Certificate",
            rows[0]["name"],
            {
                "status": "Revoked",
                "overlap_until": None,
                "revoked_at": _database_time(_current(now)),
                "revoked_by": actor,
                "revocation_reason": reason,
            },
            update_modified=False,
        )
