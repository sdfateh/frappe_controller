"""Fixed-route controller service functions for authenticated agents."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from ..protocol_negotiation import (
    NEGOTIATION_VERSION,
    REVIEWED_PROTOCOL_VERSIONS,
    NoCommonProtocolVersion,
    ProtocolNegotiationError,
    select_protocol_version,
)

from ..controller_store import ControllerStore
from ..security import (
    AUDIENCE,
    ControllerRequestError,
    TrustedPeerIdentity,
    parse_exact_json,
    require_agent_id,
    timestamp,
    validate_common,
)

_NEGOTIATION_FIELDS = frozenset(
    {"negotiation_version", "agent_id", "audience", "supported_protocol_versions"}
)
_HEARTBEAT_FIELDS = frozenset({"protocol_version", "agent_id", "audience", "state"})
_POLL_FIELDS = frozenset({"protocol_version", "agent_id", "audience"})
_RESULT_FIELDS = frozenset({"protocol_version", "agent_id", "audience", "operation_id", "result"})
_EVENT_FIELDS = frozenset({"protocol_version", "agent_id", "audience", "operation_id", "events"})


class InventoryHook(Protocol):
    def __call__(self, agent_id: str, state: Mapping[str, Any], now: datetime) -> None: ...


class EventIngestionHook(Protocol):
    def __call__(self, agent_id: str, operation_id: str, events: list[Any], now: datetime) -> int: ...


class ResultIngestionHook(Protocol):
    def __call__(
        self,
        agent_id: str,
        operation_id: str,
        result: Mapping[str, Any],
        now: datetime,
    ) -> None: ...


def _authenticated(
    store: ControllerStore,
    path_agent_id: str,
    body: Mapping[str, Any],
    peer: TrustedPeerIdentity,
    now: datetime,
) -> str:
    agent_id = validate_common(body, path_agent_id)
    store.authenticate_peer(peer, agent_id, body["agent_id"], now=now)
    return agent_id


def negotiate_protocol(
    store: ControllerStore,
    path_agent_id: str,
    raw_body: bytes | str | Mapping[str, Any],
    peer: TrustedPeerIdentity,
    *,
    reviewed_versions: tuple[str, ...] = REVIEWED_PROTOCOL_VERSIONS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authenticate an exact negotiation request and select the highest common version."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    body = parse_exact_json(raw_body, _NEGOTIATION_FIELDS, limit=4096)
    if body["negotiation_version"] != NEGOTIATION_VERSION:
        raise ControllerRequestError("unsupported_negotiation_contract")
    try:
        path_agent_id = require_agent_id(path_agent_id)
    except ValueError:
        raise ControllerRequestError("invalid_agent", 404) from None
    if body["audience"] != AUDIENCE:
        raise ControllerRequestError("wrong_audience", 403)
    if body["agent_id"] != path_agent_id:
        raise ControllerRequestError("agent_binding_mismatch", 403)
    store.authenticate_peer(
        peer,
        path_agent_id,
        body["agent_id"],
        now=current,
    )
    try:
        selected = select_protocol_version(
            body["supported_protocol_versions"],
            reviewed_versions,
        )
    except NoCommonProtocolVersion:
        raise ControllerRequestError("no_common_protocol", 409) from None
    except ProtocolNegotiationError:
        raise ControllerRequestError("invalid_supported_protocol_versions") from None
    return {
        "negotiation_version": NEGOTIATION_VERSION,
        "agent_id": path_agent_id,
        "audience": AUDIENCE,
        "controller_protocol_versions": list(reviewed_versions),
        "selected_protocol_version": selected,
    }


def heartbeat(
    store: ControllerStore,
    path_agent_id: str,
    raw_body: bytes | str | Mapping[str, Any],
    peer: TrustedPeerIdentity,
    *,
    inventory_hook: InventoryHook | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    body = parse_exact_json(raw_body, _HEARTBEAT_FIELDS)
    agent_id = _authenticated(store, path_agent_id, body, peer, current)
    state = body["state"]
    if not isinstance(state, Mapping):
        raise ControllerRequestError("invalid_heartbeat_state")
    store.record_heartbeat(agent_id, state, now=current)
    if inventory_hook is not None:
        inventory_hook(agent_id, state, current)
    return {"accepted": True, "server_time": timestamp(current)}


def poll_commands(
    store: ControllerStore,
    path_agent_id: str,
    raw_body: bytes | str | Mapping[str, Any],
    peer: TrustedPeerIdentity,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    body = parse_exact_json(raw_body, _POLL_FIELDS)
    agent_id = _authenticated(store, path_agent_id, body, peer, current)
    return {"command": store.lease_command(agent_id, now=current)}


def submit_result(
    store: ControllerStore,
    path_agent_id: str,
    raw_body: bytes | str | Mapping[str, Any],
    peer: TrustedPeerIdentity,
    *,
    ingestion_hook: ResultIngestionHook | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    body = parse_exact_json(raw_body, _RESULT_FIELDS)
    agent_id = _authenticated(store, path_agent_id, body, peer, current)
    try:
        operation_id = str(uuid.UUID(body["operation_id"]))
    except (ValueError, TypeError, AttributeError):
        raise ControllerRequestError("invalid_operation_id") from None
    if operation_id != body["operation_id"].lower() or not isinstance(body["result"], Mapping):
        raise ControllerRequestError("invalid_result")
    store.ingest_result(agent_id, operation_id, body["result"], now=current)
    if ingestion_hook is not None:
        ingestion_hook(agent_id, operation_id, body["result"], current)
    return {"accepted": True}


def submit_events(
    store: ControllerStore,
    path_agent_id: str,
    raw_body: bytes | str | Mapping[str, Any],
    peer: TrustedPeerIdentity,
    *,
    ingestion_hook: EventIngestionHook,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authenticate the event route and delegate durable ingestion semantics."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    body = parse_exact_json(raw_body, _EVENT_FIELDS)
    agent_id = _authenticated(store, path_agent_id, body, peer, current)
    try:
        operation_id = str(uuid.UUID(body["operation_id"]))
    except (ValueError, TypeError, AttributeError):
        raise ControllerRequestError("invalid_operation_id") from None
    events = body["events"]
    if not isinstance(events, list) or not events or len(events) > 100:
        raise ControllerRequestError("invalid_event_batch")
    through = ingestion_hook(agent_id, operation_id, events, current)
    if not isinstance(through, int) or isinstance(through, bool) or through < 1:
        raise ControllerRequestError("event_ingestion_failed", 500)
    return {"accepted": True, "through_sequence": through}
