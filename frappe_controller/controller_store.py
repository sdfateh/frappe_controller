"""Dependency-free SQLite reference store for controller transport semantics."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .security import (
    AUDIENCE,
    PROTOCOL_VERSION,
    CertificateIssuance,
    ControllerRequestError,
    TrustedPeerIdentity,
    VerifiedCSR,
    canonical_json,
    require_agent_id,
    sha256_json,
    timestamp,
    utc,
    validate_json,
)

_ENVELOPE_FIELDS = frozenset({
    "protocol_version", "operation_id", "idempotency_key", "agent_id", "audience",
    "bench_id", "site_id", "operation", "payload", "requested_by",
    "approval_claims", "issued_at", "expires_at", "payload_hash",
})
_TERMINAL = frozenset({
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "needs_intervention",
    "dead_letter",
    "rejected",
})
_RESULT_STATES = frozenset({"queued", "running"}) | _TERMINAL
def _now() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return timestamp(value)


def _from_db(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class ControllerStore:
    def __init__(
        self,
        path: str | Path,
        *,
        token_pepper: bytes,
        audience: str = AUDIENCE,
        command_lifetime_seconds: int = 300,
        clock_skew_seconds: int = 30,
    ) -> None:
        if not isinstance(token_pepper, bytes) or len(token_pepper) < 32:
            raise ValueError("token pepper must contain at least 32 bytes")
        if command_lifetime_seconds < 1 or clock_skew_seconds < 0:
            raise ValueError("command timing policy is invalid")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pepper = token_pepper
        self.audience = audience
        self.command_lifetime_seconds = command_lifetime_seconds
        self.clock_skew_seconds = clock_skew_seconds
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=10000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        with self._lock:
            self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS agents(
              agent_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
              expected_public_key_sha256 TEXT NOT NULL, created_by TEXT NOT NULL,
              created_at TEXT NOT NULL, last_seen_at TEXT, heartbeat_json TEXT,
              heartbeat_hash TEXT);
            CREATE TABLE IF NOT EXISTS enrollment_tokens(
              token_hash TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
              public_key_sha256 TEXT NOT NULL, expires_at TEXT NOT NULL,
              created_by TEXT NOT NULL, created_at TEXT NOT NULL, consumed_at TEXT,
              completed_at TEXT, FOREIGN KEY(agent_id) REFERENCES agents(agent_id));
            CREATE TABLE IF NOT EXISTS certificates(
              serial TEXT PRIMARY KEY, fingerprint_sha256 TEXT NOT NULL UNIQUE,
              agent_id TEXT NOT NULL, public_key_sha256 TEXT NOT NULL,
              not_before TEXT NOT NULL, not_after TEXT NOT NULL,
              status TEXT NOT NULL, overlap_until TEXT, issued_by TEXT NOT NULL,
              issued_at TEXT NOT NULL, revoked_by TEXT, revoked_at TEXT,
              FOREIGN KEY(agent_id) REFERENCES agents(agent_id),
              CHECK(status IN ('active','rotating','revoked')));
            CREATE TABLE IF NOT EXISTS commands(
              operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
              agent_id TEXT NOT NULL, immutable_json TEXT NOT NULL,
              immutable_hash TEXT NOT NULL, envelope_json TEXT NOT NULL,
              status TEXT NOT NULL, lease_expires_at TEXT, result_state TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              FOREIGN KEY(agent_id) REFERENCES agents(agent_id),
              CHECK(status IN ('approved','leased','acknowledged','terminal')));
            CREATE INDEX IF NOT EXISTS command_poll_idx
              ON commands(agent_id,status,lease_expires_at,created_at);
            CREATE TABLE IF NOT EXISTS result_submissions(
              operation_id TEXT NOT NULL, body_hash TEXT NOT NULL,
              result_json TEXT NOT NULL, result_state TEXT NOT NULL,
              received_at TEXT NOT NULL, PRIMARY KEY(operation_id,body_hash),
              FOREIGN KEY(operation_id) REFERENCES commands(operation_id));
            """)

    class _Transaction:
        def __init__(self, owner: "ControllerStore") -> None:
            self.owner = owner
        def __enter__(self) -> sqlite3.Connection:
            self.owner._lock.acquire()
            self.owner._connection.execute("BEGIN IMMEDIATE")
            return self.owner._connection
        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            try:
                self.owner._connection.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.owner._lock.release()

    def _transaction(self) -> "ControllerStore._Transaction":
        return self._Transaction(self)

    def create_agent(self, agent_id: str, expected_public_key_sha256: str, *, actor: str, now: datetime | None = None) -> None:
        require_agent_id(agent_id)
        if len(expected_public_key_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_public_key_sha256):
            raise ValueError("expected public key digest is invalid")
        with self._transaction() as db:
            db.execute("INSERT INTO agents VALUES(?,1,?,?,?,NULL,NULL,NULL)", (agent_id, expected_public_key_sha256, actor, _db_time(utc(now or _now()))))

    def set_agent_enabled(self, agent_id: str, enabled: bool) -> None:
        with self._transaction() as db:
            changed = db.execute("UPDATE agents SET enabled=? WHERE agent_id=?", (int(enabled), agent_id)).rowcount
            if changed != 1:
                raise ControllerRequestError("unknown_agent", 404)

    def _token_hash(self, raw_token: str) -> str:
        return hmac.new(self._pepper, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()

    def create_enrollment_token(self, agent_id: str, *, actor: str, ttl_seconds: int = 600, now: datetime | None = None) -> str:
        if ttl_seconds < 1 or ttl_seconds > 3600:
            raise ValueError("enrollment token lifetime is invalid")
        issued = utc(now or _now())
        raw_token = secrets.token_urlsafe(32)
        with self._transaction() as db:
            agent = db.execute("SELECT * FROM agents WHERE agent_id=? AND enabled=1", (agent_id,)).fetchone()
            if agent is None:
                raise ControllerRequestError("unknown_or_disabled_agent", 404)
            db.execute(
                "INSERT INTO enrollment_tokens VALUES(?,?,?,?,?,?,NULL,NULL)",
                (self._token_hash(raw_token), agent_id, agent["expected_public_key_sha256"], _db_time(issued + timedelta(seconds=ttl_seconds)), actor, _db_time(issued)),
            )
        return raw_token

    def claim_enrollment_token(
        self, raw_token: str, verified: VerifiedCSR, *, now: datetime | None = None
    ) -> None:
        """Atomically consume a valid bound token before external CA issuance."""
        current = utc(now or _now())
        with self._transaction() as db:
            token = db.execute(
                "SELECT * FROM enrollment_tokens WHERE token_hash=?",
                (self._token_hash(raw_token),),
            ).fetchone()
            if token is None or token["agent_id"] != verified.agent_id:
                raise ControllerRequestError("invalid_enrollment_token", 403)
            if token["consumed_at"] is not None:
                raise ControllerRequestError("enrollment_token_used", 409)
            if _from_db(token["expires_at"]) <= current:
                raise ControllerRequestError("enrollment_token_expired", 403)
            if not hmac.compare_digest(
                token["public_key_sha256"], verified.public_key_sha256
            ):
                raise ControllerRequestError("csr_key_mismatch", 403)
            agent = db.execute(
                "SELECT enabled FROM agents WHERE agent_id=?", (verified.agent_id,)
            ).fetchone()
            if agent is None or not agent["enabled"]:
                raise ControllerRequestError("unknown_or_disabled_agent", 403)
            changed = db.execute(
                "UPDATE enrollment_tokens SET consumed_at=? "
                "WHERE token_hash=? AND consumed_at IS NULL",
                (_db_time(current), token["token_hash"]),
            ).rowcount
            if changed != 1:
                raise ControllerRequestError("enrollment_token_used", 409)

    def complete_enrollment(
        self,
        raw_token: str,
        verified: VerifiedCSR,
        issuance: CertificateIssuance,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> None:
        current = utc(now or _now())
        if issuance.public_key_sha256 != verified.public_key_sha256:
            raise ControllerRequestError("certificate_key_mismatch", 409)
        with self._transaction() as db:
            token = db.execute(
                "SELECT * FROM enrollment_tokens WHERE token_hash=?",
                (self._token_hash(raw_token),),
            ).fetchone()
            if (
                token is None
                or token["agent_id"] != verified.agent_id
                or token["consumed_at"] is None
                or token["completed_at"] is not None
            ):
                raise ControllerRequestError("enrollment_completion_conflict", 409)
            if not hmac.compare_digest(
                token["public_key_sha256"], verified.public_key_sha256
            ):
                raise ControllerRequestError("csr_key_mismatch", 403)
            self._insert_certificate(db, verified.agent_id, issuance, actor, current)
            db.execute(
                "UPDATE enrollment_tokens SET completed_at=? WHERE token_hash=?",
                (_db_time(current), token["token_hash"]),
            )

    @staticmethod
    def _insert_certificate(db: sqlite3.Connection, agent_id: str, issuance: CertificateIssuance, actor: str, now: datetime) -> None:
        if not (utc(issuance.not_before) <= now < utc(issuance.not_after)):
            raise ControllerRequestError("certificate_not_current", 409)
        db.execute(
            "INSERT INTO certificates VALUES(?,?,?,?,?,?,'active',NULL,?,?,NULL,NULL)",
            (issuance.serial, issuance.fingerprint_sha256, agent_id, issuance.public_key_sha256, _db_time(issuance.not_before), _db_time(issuance.not_after), actor, _db_time(now)),
        )

    def authenticate_peer(self, peer: TrustedPeerIdentity, path_agent_id: str, body_agent_id: str, *, now: datetime | None = None) -> None:
        del now  # no certificate lifetime to check; identity is trusted from the body
        if type(peer) is not TrustedPeerIdentity:
            raise ControllerRequestError("untrusted_peer_identity", 401)
        if peer.agent_id != path_agent_id or body_agent_id != path_agent_id:
            raise ControllerRequestError("agent_binding_mismatch", 403)

    def rotate_certificate(self, peer: TrustedPeerIdentity, verified: VerifiedCSR, issuance: CertificateIssuance, *, actor: str, overlap_seconds: int = 120, now: datetime | None = None) -> None:
        if not 0 <= overlap_seconds <= 300:
            raise ValueError("certificate overlap exceeds policy")
        current = utc(now or _now())
        self.authenticate_peer(peer, verified.agent_id, verified.agent_id, now=current)
        if issuance.public_key_sha256 != verified.public_key_sha256:
            raise ControllerRequestError("certificate_key_mismatch", 409)
        with self._transaction() as db:
            changed = db.execute(
                "UPDATE certificates SET status='rotating',overlap_until=? WHERE serial=? AND agent_id=? AND status='active'",
                (_db_time(current + timedelta(seconds=overlap_seconds)), peer.certificate_serial, verified.agent_id),
            ).rowcount
            if changed != 1:
                raise ControllerRequestError("certificate_rotation_conflict", 409)
            self._insert_certificate(db, verified.agent_id, issuance, actor, current)

    def revoke_certificate(self, agent_id: str, serial: str, *, actor: str, now: datetime | None = None) -> None:
        with self._transaction() as db:
            changed = db.execute(
                "UPDATE certificates SET status='revoked',overlap_until=NULL,revoked_by=?,revoked_at=? WHERE agent_id=? AND serial=? AND status!='revoked'",
                (actor, _db_time(utc(now or _now())), agent_id, serial),
            ).rowcount
            if changed != 1:
                raise ControllerRequestError("certificate_not_found", 404)

    def record_heartbeat(self, agent_id: str, state: Mapping[str, Any], *, now: datetime | None = None) -> str:
        validate_json(state, reject_sensitive=True, reject_routing=True)
        encoded = canonical_json(state)
        if len(encoded.encode("utf-8")) > 512 * 1024:
            raise ControllerRequestError("heartbeat_too_large", 413)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._transaction() as db:
            changed = db.execute(
                "UPDATE agents SET last_seen_at=?,heartbeat_json=?,heartbeat_hash=? WHERE agent_id=? AND enabled=1",
                (_db_time(utc(now or _now())), encoded, digest, agent_id),
            ).rowcount
            if changed != 1:
                raise ControllerRequestError("unknown_or_disabled_agent", 403)
        return digest

    def enqueue_approved_command(self, envelope: Mapping[str, Any], *, now: datetime | None = None) -> None:
        if frozenset(envelope) != _ENVELOPE_FIELDS:
            raise ValueError("command envelope fields are invalid")
        validate_json(envelope)
        if envelope["protocol_version"] != PROTOCOL_VERSION or envelope["audience"] != self.audience:
            raise ValueError("command envelope protocol is invalid")
        agent_id = require_agent_id(envelope["agent_id"])
        operation_id = str(uuid.UUID(envelope["operation_id"]))
        if operation_id != envelope["operation_id"].lower() or not envelope["idempotency_key"].endswith(operation_id):
            raise ValueError("command identity is invalid")
        payload = envelope["payload"]
        if not isinstance(payload, Mapping):
            raise ValueError("command payload must be an object")
        validate_json(payload, reject_sensitive=True)
        expected_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        if not hmac.compare_digest(expected_hash, envelope["payload_hash"]):
            raise ValueError("command payload hash is invalid")
        immutable = {key: value for key, value in envelope.items() if key not in {"issued_at", "expires_at"}}
        immutable_json = canonical_json(immutable)
        created = utc(now or _now())
        with self._transaction() as db:
            agent = db.execute("SELECT enabled FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
            if agent is None or not agent["enabled"]:
                raise ControllerRequestError("unknown_or_disabled_agent", 403)
            try:
                db.execute(
                    "INSERT INTO commands VALUES(?,?,?,?,?,?,'approved',NULL,NULL,?,?)",
                    (operation_id, envelope["idempotency_key"], agent_id, immutable_json, hashlib.sha256(immutable_json.encode()).hexdigest(), canonical_json(envelope), _db_time(created), _db_time(created)),
                )
            except sqlite3.IntegrityError:
                row = db.execute("SELECT immutable_hash FROM commands WHERE operation_id=? OR idempotency_key=?", (operation_id, envelope["idempotency_key"])).fetchone()
                if row is None or not hmac.compare_digest(row["immutable_hash"], hashlib.sha256(immutable_json.encode()).hexdigest()):
                    raise ControllerRequestError("command_identity_conflict", 409) from None

    def lease_command(self, agent_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        current = utc(now or _now())
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM commands WHERE agent_id=? AND (status='approved' OR (status='leased' AND lease_expires_at<=?)) ORDER BY created_at,operation_id LIMIT 1",
                (agent_id, _db_time(current)),
            ).fetchone()
            if row is None:
                return None
            envelope = json.loads(row["envelope_json"])
            envelope["issued_at"] = timestamp(current)
            expires = current + timedelta(seconds=self.command_lifetime_seconds)
            envelope["expires_at"] = timestamp(expires)
            db.execute(
                "UPDATE commands SET status='leased',lease_expires_at=?,envelope_json=?,updated_at=? WHERE operation_id=?",
                (_db_time(expires + timedelta(seconds=self.clock_skew_seconds)), canonical_json(envelope), _db_time(current), row["operation_id"]),
            )
            return envelope

    def ingest_result(self, agent_id: str, operation_id: str, result: Mapping[str, Any], *, now: datetime | None = None) -> bool:
        validate_json(result, reject_sensitive=True)
        encoded = canonical_json(result)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ControllerRequestError("result_too_large", 413)
        state = result.get("state", result.get("status"))
        if state not in _RESULT_STATES:
            raise ControllerRequestError("invalid_result_state")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        current = utc(now or _now())
        with self._transaction() as db:
            command = db.execute("SELECT * FROM commands WHERE operation_id=?", (operation_id,)).fetchone()
            if command is None or command["agent_id"] != agent_id:
                raise ControllerRequestError("operation_ownership_mismatch", 403)
            duplicate = db.execute("SELECT 1 FROM result_submissions WHERE operation_id=? AND body_hash=?", (operation_id, digest)).fetchone()
            if duplicate is not None:
                return False
            prior = command["result_state"]
            if prior in _TERMINAL:
                raise ControllerRequestError("terminal_result_conflict", 409)
            if prior == "running" and state == "queued":
                raise ControllerRequestError("result_regression", 409)
            db.execute("INSERT INTO result_submissions VALUES(?,?,?,?,?)", (operation_id, digest, encoded, state, _db_time(current)))
            status = "terminal" if state in _TERMINAL else "acknowledged"
            db.execute("UPDATE commands SET status=?,lease_expires_at=NULL,result_state=?,updated_at=? WHERE operation_id=?", (status, state, _db_time(current), operation_id))
            return True

    def certificate_rows(self, agent_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute("SELECT * FROM certificates WHERE agent_id=? ORDER BY issued_at,serial", (agent_id,)).fetchall()
