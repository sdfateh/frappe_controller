"""Frappe-backed authoritative command queue and lease store."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

try:
    from .dispatcher import (
        ApprovalBoundDispatcher,
        DispatchConflict,
        immutable_command,
    )
    from .security import ControllerRequestError, canonical_json, timestamp
except ImportError:  # pragma: no cover
    from dispatcher import ApprovalBoundDispatcher, DispatchConflict, immutable_command  # type: ignore
    from security import ControllerRequestError, canonical_json, timestamp  # type: ignore


def _runtime() -> Any:
    import frappe

    return frappe


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("lease time must be a datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class FrappeCommandStore:
    """Queue approved operations and atomically lease them to their owning agent.

    The class intentionally covers only command persistence.  Certificate and
    enrollment methods live in the security store and may be composed with this
    adapter by the route layer.
    """

    def __init__(
        self,
        frappe_module: Any | None = None,
        *,
        dispatcher: ApprovalBoundDispatcher | None = None,
        command_lifetime_seconds: int = 300,
        clock_skew_seconds: int = 30,
    ) -> None:
        if command_lifetime_seconds < 1 or command_lifetime_seconds > 300 or clock_skew_seconds < 0:
            raise ValueError("command timing policy is invalid")
        self.frappe = frappe_module or _runtime()
        self.db = self.frappe.db
        self.command_lifetime_seconds = command_lifetime_seconds
        self.clock_skew_seconds = clock_skew_seconds
        self.dispatcher = dispatcher or ApprovalBoundDispatcher(
            self.frappe, command_lifetime_seconds=command_lifetime_seconds
        )

    def enqueue_approved_operation(self, operation_name: str, *, now: datetime) -> dict[str, Any]:
        return self.dispatcher.dispatch(operation_name, now=_utc(now))

    def enqueue_approved_command(self, envelope: Mapping[str, Any], *, now: datetime | None = None) -> None:
        """ControllerStore-compatible entry point without allowing approval bypass."""
        operation_id = envelope.get("operation_id")
        if not isinstance(operation_id, str):
            raise ValueError("command operation_id is invalid")
        current = _utc(now or datetime.now(UTC))
        approved = self.dispatcher.dispatch(operation_id, now=current)
        if immutable_command(approved) != immutable_command(envelope):
            raise DispatchConflict("supplied command differs from approved operation")

    def lease_command(
        self,
        agent_id: str,
        *,
        now: datetime | None = None,
        allowed_operation_types: tuple[str, ...] | None = None,
        command_filter: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> dict[str, Any] | None:
        current = _utc(now or datetime.now(UTC))
        operation_clause = ""
        parameters: list[Any] = [agent_id, current]
        if allowed_operation_types is not None:
            if not allowed_operation_types:
                return None
            operation_clause = " AND o.operation_type IN (" + ",".join(
                ["%s"] * len(allowed_operation_types)
            ) + ")"
            parameters.extend(allowed_operation_types)
        limit = 1001 if command_filter is not None else 1
        rows = self.db.sql(
            "SELECT o.name, o.command_json, o.command_hash, o.operation_type, "
            "o.payload_json, o.bulk_parent, "
            "COALESCE(bt.environment_snapshot,s.environment,a.environment) "
            "AS target_environment FROM `tabOperation` o "
            "JOIN `tabServer Agent` a ON a.name=o.server_agent "
            "LEFT JOIN `tabManaged Site` s ON s.name=o.managed_site "
            "LEFT JOIN `tabBulk Operation Target` bt ON bt.name=o.bulk_target "
            "WHERE a.agent_id=%s AND a.enabled=1 AND COALESCE(a.drain_requested,0)=0 "
            "AND o.command_hash IS NOT NULL "
            "AND (o.state='queued' OR (o.state='leased' AND o.lease_expires_at<=%s)) "
            f"{operation_clause} ORDER BY o.creation, o.name LIMIT {limit} "
            "FOR UPDATE SKIP LOCKED",
            tuple(parameters),
            as_dict=True,
        )
        operation = next(
            (
                row for row in rows
                if command_filter is None or command_filter(row)
            ),
            None,
        )
        if operation is None:
            return None
        try:
            envelope = self.dispatcher.validate_persisted(
                operation["name"], operation["command_json"], operation["command_hash"], now=current
            )
        except DispatchConflict as exc:
            raise ControllerRequestError("command_approval_conflict", 409) from exc
        expires = current + timedelta(seconds=self.command_lifetime_seconds)
        envelope["issued_at"] = timestamp(current)
        envelope["expires_at"] = timestamp(expires)
        self.db.set_value(
            "Operation",
            operation["name"],
            {
                "state": "leased",
                "issued_at": current,
                "expires_at": expires,
                "lease_expires_at": expires + timedelta(seconds=self.clock_skew_seconds),
            },
            update_modified=False,
        )
        self.db.set_value(
            "Operation Target", {"operation": operation["name"]}, "state", "leased",
            update_modified=False,
        )
        return json.loads(canonical_json(envelope))
