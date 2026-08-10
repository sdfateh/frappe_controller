"""Strict append-only controller event and monotonic result ingestion."""
from __future__ import annotations

import hashlib, json, re, uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Protocol, Sequence

PROTOCOL_VERSION = "1.0"; CONTROLLER_AUDIENCE = "frappe-controller"
_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$"); _STEP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"); _ERROR = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "needs_intervention", "dead_letter", "rejected"}; _STATES = _TERMINAL | {"queued", "running"}
_SENSITIVE = ("password", "passwd", "secret", "token", "credential", "private_key", "authorization")

class IngestionError(RuntimeError): pass
class IngestionSchemaError(IngestionError): pass
class IngestionOwnershipError(IngestionError): pass
class IngestionConflictError(IngestionError): pass
class UnknownOperationError(IngestionError): pass


def _obj(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value): raise IngestionSchemaError(f"{name} must be an object")
    return value
def _exact(value: Mapping[str, Any], fields: set[str], name: str) -> None:
    if set(value) != fields: raise IngestionSchemaError(f"{name} fields do not match protocol v1")
def _text(value: object, name: str, limit: int = 253) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or "\n" in value or len(value.encode()) > limit: raise IngestionSchemaError(f"{name} is invalid")
    return value
def _uuid(value: object, name: str = "operation_id") -> str:
    text = _text(value, name, 36)
    try: parsed = uuid.UUID(text)
    except ValueError: raise IngestionSchemaError(f"{name} must be a UUID") from None
    if str(parsed) != text.lower(): raise IngestionSchemaError(f"{name} must be canonical")
    return text.lower()
def _time(value: object, name: str) -> datetime:
    text = _text(value, name, 64)
    try: parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError: raise IngestionSchemaError(f"{name} must be RFC3339") from None
    if parsed.tzinfo is None: raise IngestionSchemaError(f"{name} requires timezone")
    return parsed.astimezone(UTC)
def _received(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None: raise IngestionSchemaError("received_at must be aware")
    return value.astimezone(UTC)
def _sensitive(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(any(marker in str(key).lower().replace("-", "_") for marker in _SENSITIVE) or _sensitive(child) for key, child in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)): return any(_sensitive(item) for item in value)
    return False
def _canonical(value: object, name: str, limit: int) -> tuple[str, str]:
    if _sensitive(value): raise IngestionSchemaError(f"{name} contains raw secret field")
    try: encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError): raise IngestionSchemaError(f"{name} must be JSON") from None
    if len(encoded.encode()) > limit: raise IngestionSchemaError(f"{name} too large")
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    operation_id: str; agent_id: str; bench_id: str; site_domain: str | None; operation_type: str; state: str; last_event_sequence: int
@dataclass(frozen=True, slots=True)
class StoredEvent:
    operation_id: str; sequence: int; body_hash: str
@dataclass(frozen=True, slots=True)
class IngestedEvent:
    operation_id: str; sequence: int; attempt: int | None; step: str | None; kind: str; details_json: str | None; body_hash: str; agent_created_at: datetime; received_at: datetime
@dataclass(frozen=True, slots=True)
class StoredResult:
    operation_id: str; status: str; body_hash: str; result_json: str
@dataclass(frozen=True, slots=True)
class IngestedResult:
    operation_id: str; status: str; body_hash: str; result_json: str; error_code: str | None; received_at: datetime


class IngestionRepository(Protocol):
    def operation(self, operation_id: str) -> OperationIdentity | None: ...
    def event(self, operation_id: str, sequence: int) -> StoredEvent | None: ...
    def append_events(self, agent_id: str, operation_id: str, *, expected_last_sequence: int, events: tuple[IngestedEvent, ...]) -> int: ...
    def result(self, operation_id: str) -> StoredResult | None: ...
    def commit_result(self, agent_id: str, operation_id: str, *, expected_previous_hash: str | None, result: IngestedResult) -> None: ...


def _common(request: object, fields: set[str]) -> tuple[Mapping[str, Any], str, str]:
    raw = _obj(request, "request"); _exact(raw, fields, "request")
    if raw["protocol_version"] != PROTOCOL_VERSION: raise IngestionSchemaError("protocol version mismatch")
    agent = _text(raw["agent_id"], "agent_id", 128)
    if raw["audience"] != CONTROLLER_AUDIENCE: raise IngestionSchemaError("audience mismatch")
    return raw, agent, _uuid(raw["operation_id"])


def _owner(repo: IngestionRepository, agent: str, operation_id: str) -> OperationIdentity:
    operation = repo.operation(operation_id)
    if operation is None: raise UnknownOperationError("unknown operation")
    if operation.agent_id != agent: raise IngestionOwnershipError("operation belongs to another agent")
    return operation


class ControllerIngestionService:
    def __init__(self, repository: IngestionRepository): self._repository = repository

    def ingest_events(self, request: object, *, received_at: datetime) -> int:
        now = _received(received_at); raw, agent, operation_id = _common(request, {"protocol_version", "agent_id", "audience", "operation_id", "events"}); operation = _owner(self._repository, agent, operation_id)
        values = raw["events"]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)) or not values or len(values) > 250: raise IngestionSchemaError("events must be non-empty bounded array")
        parsed = tuple(self._event(operation_id, item, now, i) for i, item in enumerate(values)); sequences = [item.sequence for item in parsed]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)): raise IngestionSchemaError("events must be strictly ordered")
        cursor = operation.last_event_sequence; pending = []
        for item in parsed:
            existing = self._repository.event(operation_id, item.sequence)
            if existing is not None:
                if existing.body_hash != item.body_hash: raise IngestionConflictError("event is immutable")
                if item.sequence > cursor: raise IngestionConflictError("inconsistent cursor")
                continue
            if item.sequence != cursor + 1: raise IngestionConflictError("event gap")
            pending.append(item); cursor = item.sequence
        if pending:
            committed = self._repository.append_events(agent, operation_id, expected_last_sequence=operation.last_event_sequence, events=tuple(pending))
            if committed != cursor: raise IngestionConflictError("invalid committed cursor")
        return cursor

    @staticmethod
    def _event(operation_id: str, value: object, now: datetime, index: int) -> IngestedEvent:
        name = f"events[{index}]"; raw = _obj(value, name); _exact(raw, {"sequence", "attempt", "step", "kind", "details", "created_at"}, name)
        sequence, attempt, step = raw["sequence"], raw["attempt"], raw["step"]
        if type(sequence) is not int or sequence < 1: raise IngestionSchemaError("invalid event sequence")
        if attempt is not None and (type(attempt) is not int or attempt < 0): raise IngestionSchemaError("invalid event attempt")
        if step is not None and (not _STEP.fullmatch(_text(step, "step", 128))): raise IngestionSchemaError("invalid event step")
        kind = _text(raw["kind"], "kind", 128)
        if not _KIND.fullmatch(kind): raise IngestionSchemaError("invalid event kind")
        created = _time(raw["created_at"], "created_at")
        if created > now + timedelta(minutes=5): raise IngestionSchemaError("future event")
        details_json = None if raw["details"] is None else _canonical(raw["details"], "details", 65536)[0]
        body = {"sequence": sequence, "attempt": attempt, "step": step, "kind": kind, "details": raw["details"], "created_at": created.isoformat()}; body_hash = _canonical(body, name, 67584)[1]
        return IngestedEvent(operation_id, sequence, attempt, step, kind, details_json, body_hash, created, now)

    def ingest_result(self, request: object, *, received_at: datetime) -> IngestedResult:
        now = _received(received_at); raw, agent, operation_id = _common(request, {"protocol_version", "agent_id", "audience", "operation_id", "result"}); operation = _owner(self._repository, agent, operation_id); result = self._result(operation, raw["result"], now)
        existing = self._repository.result(operation_id)
        if existing is not None and existing.body_hash == result.body_hash: return result
        if existing is not None and existing.status in _TERMINAL: raise IngestionConflictError("terminal result immutable")
        if existing is not None and not ((existing.status == "queued" and (result.status == "running" or result.status in _TERMINAL)) or (existing.status == "running" and result.status in _TERMINAL)): raise IngestionConflictError("non-monotonic result")
        self._repository.commit_result(agent, operation_id, expected_previous_hash=None if existing is None else existing.body_hash, result=result); return result

    def _result(self, operation: OperationIdentity, value: object, now: datetime) -> IngestedResult:
        raw = _obj(value, "result"); allowed = {"status", "operation_id", "bench_id", "site_domain", "operation", "attempt", "max_attempts", "error_code", "result", "accepted", "target_operation_id"}
        if set(raw) - allowed or "status" not in raw: raise IngestionSchemaError("invalid result fields")
        status = _text(raw["status"], "status", 32)
        if status not in _STATES: raise IngestionSchemaError("unsupported result state")
        error = raw.get("error_code")
        if error is not None and not _ERROR.fullmatch(_text(error, "error_code", 128)): raise IngestionSchemaError("invalid error_code")
        if status == "rejected":
            if set(raw) != {"status", "error_code"} or error is None: raise IngestionSchemaError("invalid rejected result")
        elif set(raw) == {"status", "error_code"}:
            if status != "failed" or error is None: raise IngestionSchemaError("invalid short result")
        else:
            required = {"status", "operation_id", "bench_id", "site_domain", "operation", "attempt", "max_attempts", "error_code"}
            if not required <= set(raw): raise IngestionSchemaError("missing result identity")
            nested = _uuid(raw["operation_id"], "result.operation_id")
            if operation.operation_type == "operation.cancel":
                target = raw.get("target_operation_id")
                if target is None or _uuid(target, "target_operation_id") != nested: raise IngestionOwnershipError("cancel target mismatch")
                target_op = _owner(self._repository, operation.agent_id, nested)
                if target_op.bench_id != operation.bench_id: raise IngestionOwnershipError("cancel bench mismatch")
            elif nested != operation.operation_id: raise IngestionOwnershipError("result operation mismatch")
            if raw["bench_id"] != operation.bench_id: raise IngestionOwnershipError("result bench mismatch")
            if operation.operation_type != "operation.cancel" and (raw["site_domain"] != operation.site_domain or raw["operation"] != operation.operation_type): raise IngestionOwnershipError("result target mismatch")
            if type(raw["attempt"]) is not int or raw["attempt"] < 0 or type(raw["max_attempts"]) is not int or raw["max_attempts"] < 1: raise IngestionSchemaError("invalid attempt")
            if "accepted" in raw and type(raw["accepted"]) is not bool: raise IngestionSchemaError("accepted must be boolean")
        result_json, body_hash = _canonical(raw, "result", 262144)
        return IngestedResult(operation.operation_id, status, body_hash, result_json, error, now)
