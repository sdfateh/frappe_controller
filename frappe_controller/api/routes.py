"""Real Frappe adapters for the four fixed controller-agent routes.

The public reverse proxy maps the frozen ``/v1/agents/...`` paths to these
whitelisted POST methods.  Each method ignores Frappe form arguments, derives
the path and peer identity only from the authenticated proxy context, and
returns a Werkzeug response so Frappe does not wrap the protocol body in a
``message`` property.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

import frappe
from werkzeug.wrappers import Response

from ..dispatcher import DispatchConflict
from ..frappe_repository import FrappeIngestionRepository, FrappeInventoryRepository
from ..frappe_security_store import FrappeCertificateStore
from ..frappe_store import FrappeCommandStore
from ..feature_flags import (
    command_enabled_for_agent,
    enabled_operations_for_agent,
)
from ..ingestion import (
    ControllerIngestionService,
    IngestionConflictError,
    IngestionOwnershipError,
    IngestionSchemaError,
    UnknownOperationError,
)
from ..integration import event_ingestion_hook, inventory_hook, result_ingestion_hook
from ..inventory import (
    InventoryConflictError,
    InventoryOwnershipError,
    InventoryReconciliationService,
    InventorySchemaError,
)
from ..proxy_security import trusted_peer_and_route_from_frappe_request
from ..security import (
    MAX_REQUEST_BYTES,
    ControllerRequestError,
    TrustedPeerIdentity,
    canonical_json,
    validate_json,
)
from .agent import (
    heartbeat,
    negotiate_protocol,
    poll_commands,
    submit_events,
    submit_result,
)


class _FrappeRouteStore:
    """Compose the security and command stores for ``api.agent``.

    Heartbeat and result persistence belongs to the injected transactional
    repositories.  These compatibility methods validate the intermediate body
    without committing a second or partial projection.
    """

    def __init__(
        self,
        certificate_store: FrappeCertificateStore,
        command_store: FrappeCommandStore,
    ) -> None:
        self._certificates = certificate_store
        self._commands = command_store

    def authenticate_peer(
        self,
        peer: TrustedPeerIdentity,
        path_agent_id: str,
        body_agent_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self._certificates.authenticate_peer(
            peer, path_agent_id, body_agent_id, now=now
        )

    def record_heartbeat(
        self,
        agent_id: str,
        state: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> str:
        del agent_id, now
        validate_json(state, reject_sensitive=True, reject_routing=True)
        encoded = canonical_json(state)
        if len(encoded.encode("utf-8")) > 512 * 1024:
            raise ControllerRequestError("heartbeat_too_large", 413)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def lease_command(
        self, agent_id: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        return self._commands.lease_command(
            agent_id,
            now=now,
            allowed_operation_types=enabled_operations_for_agent(frappe, agent_id),
            command_filter=lambda operation: command_enabled_for_agent(
                frappe, agent_id, operation
            ),
        )

    def ingest_result(
        self,
        agent_id: str,
        operation_id: str,
        result: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> bool:
        del agent_id, operation_id, now
        validate_json(result, reject_sensitive=True)
        if len(canonical_json(result).encode("utf-8")) > 256 * 1024:
            raise ControllerRequestError("result_too_large", 413)
        return True


def _configuration_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = frappe.conf.get(name, default)
    if type(raw) is not int or not minimum <= raw <= maximum:
        raise RuntimeError(f"invalid {name} configuration")
    return raw


def _components() -> tuple[
    _FrappeRouteStore,
    InventoryReconciliationService,
    ControllerIngestionService,
]:
    command_lifetime = _configuration_int(
        "frappe_controller_command_lifetime_seconds", 300, 1, 300
    )
    clock_skew = _configuration_int(
        "frappe_controller_clock_skew_seconds", 30, 0, 300
    )
    offline_after = _configuration_int(
        "frappe_controller_offline_after_seconds", 90, 1, 86_400
    )
    certificates = FrappeCertificateStore.from_environment(frappe_module=frappe)
    commands = FrappeCommandStore(
        frappe,
        command_lifetime_seconds=command_lifetime,
        clock_skew_seconds=clock_skew,
    )
    store = _FrappeRouteStore(certificates, commands)
    inventory = InventoryReconciliationService(
        FrappeInventoryRepository(frappe),
        offline_after_seconds=offline_after,
    )
    ingestion = ControllerIngestionService(FrappeIngestionRepository(frappe))
    return store, inventory, ingestion


def _request_body(limit: int = MAX_REQUEST_BYTES) -> bytes:
    request = frappe.request
    if request.method != "POST":
        raise ControllerRequestError("method_not_allowed", 405)
    if request.mimetype != "application/json":
        raise ControllerRequestError("content_type_required", 415)
    content_length = request.content_length
    if content_length is not None and content_length > limit:
        raise ControllerRequestError("request_too_large", 413)
    raw = request.get_data(cache=True)
    if len(raw) > limit:
        raise ControllerRequestError("request_too_large", 413)
    return raw


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


def _rollback() -> None:
    database = getattr(frappe, "db", None)
    if database is not None:
        database.rollback()


def _mapped_error(error: Exception) -> tuple[str, int]:
    if isinstance(error, ControllerRequestError):
        return error.code, error.status
    if isinstance(error, (InventorySchemaError, IngestionSchemaError)):
        return "invalid_protocol_body", 400
    if isinstance(error, (InventoryOwnershipError, IngestionOwnershipError)):
        return "ownership_mismatch", 403
    if isinstance(error, UnknownOperationError):
        return "unknown_operation", 404
    if isinstance(
        error,
        (InventoryConflictError, IngestionConflictError, DispatchConflict),
    ):
        return "state_conflict", 409
    return "internal_error", 500


def _dispatch(
    action: str,
    callback: Callable[
        [_FrappeRouteStore, InventoryReconciliationService, ControllerIngestionService,
         str, bytes, TrustedPeerIdentity, datetime],
        Mapping[str, Any],
    ],
) -> Response:
    try:
        peer, path_agent_id = trusted_peer_and_route_from_frappe_request(
            frappe.request, action
        )
        raw = _request_body()
        store, inventory, ingestion = _components()
        now = datetime.now(UTC)
        result = callback(
            store, inventory, ingestion, path_agent_id, raw, peer, now
        )
        return _json_response(result)
    except Exception as error:
        _rollback()
        code, status = _mapped_error(error)
        return _json_response({"accepted": False, "error": code}, status=status)


def _heartbeat(
    store: _FrappeRouteStore,
    inventory: InventoryReconciliationService,
    ingestion: ControllerIngestionService,
    agent_id: str,
    raw: bytes,
    peer: TrustedPeerIdentity,
    now: datetime,
) -> Mapping[str, Any]:
    del ingestion
    return heartbeat(
        store,
        agent_id,
        raw,
        peer,
        inventory_hook=inventory_hook(inventory),
        now=now,
    )


def _negotiate(
    store: _FrappeRouteStore,
    inventory: InventoryReconciliationService,
    ingestion: ControllerIngestionService,
    agent_id: str,
    raw: bytes,
    peer: TrustedPeerIdentity,
    now: datetime,
) -> Mapping[str, Any]:
    del inventory, ingestion
    return negotiate_protocol(store, agent_id, raw, peer, now=now)


def _poll(
    store: _FrappeRouteStore,
    inventory: InventoryReconciliationService,
    ingestion: ControllerIngestionService,
    agent_id: str,
    raw: bytes,
    peer: TrustedPeerIdentity,
    now: datetime,
) -> Mapping[str, Any]:
    del inventory, ingestion
    return poll_commands(store, agent_id, raw, peer, now=now)


def _events(
    store: _FrappeRouteStore,
    inventory: InventoryReconciliationService,
    ingestion: ControllerIngestionService,
    agent_id: str,
    raw: bytes,
    peer: TrustedPeerIdentity,
    now: datetime,
) -> Mapping[str, Any]:
    del inventory
    return submit_events(
        store,
        agent_id,
        raw,
        peer,
        ingestion_hook=event_ingestion_hook(ingestion),
        now=now,
    )


def _result(
    store: _FrappeRouteStore,
    inventory: InventoryReconciliationService,
    ingestion: ControllerIngestionService,
    agent_id: str,
    raw: bytes,
    peer: TrustedPeerIdentity,
    now: datetime,
) -> Mapping[str, Any]:
    del inventory
    return submit_result(
        store,
        agent_id,
        raw,
        peer,
        ingestion_hook=result_ingestion_hook(ingestion),
        now=now,
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])
def negotiate_protocol_route(**_request_arguments: Any) -> Response:
    return _dispatch("protocol:negotiate", _negotiate)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def heartbeat_route(**_request_arguments: Any) -> Response:
    return _dispatch("heartbeat", _heartbeat)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def poll_commands_route(**_request_arguments: Any) -> Response:
    return _dispatch("commands:poll", _poll)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def submit_events_route(**_request_arguments: Any) -> Response:
    return _dispatch("events", _events)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def submit_result_route(**_request_arguments: Any) -> Response:
    return _dispatch("results", _result)
