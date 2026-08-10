"""Adapters composing authenticated routes with controller projections."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from .ingestion import ControllerIngestionService
from .inventory import InventoryReconciliationService


def inventory_hook(
    service: InventoryReconciliationService,
) -> Callable[[str, Mapping[str, Any], datetime], None]:
    def reconcile(agent_id: str, state: Mapping[str, Any], now: datetime) -> None:
        service.reconcile(agent_id, state, observed_at=now)

    return reconcile


def event_ingestion_hook(
    service: ControllerIngestionService,
) -> Callable[[str, str, list[Any], datetime], int]:
    def ingest(
        agent_id: str,
        operation_id: str,
        events: list[Any],
        now: datetime,
    ) -> int:
        return service.ingest_events(
            {
                "protocol_version": "1.0",
                "agent_id": agent_id,
                "audience": "frappe-controller",
                "operation_id": operation_id,
                "events": events,
            },
            received_at=now,
        )

    return ingest


def result_ingestion_hook(
    service: ControllerIngestionService,
) -> Callable[[str, str, Mapping[str, Any], datetime], None]:
    def ingest(
        agent_id: str,
        operation_id: str,
        result: Mapping[str, Any],
        now: datetime,
    ) -> None:
        service.ingest_result(
            {
                "protocol_version": "1.0",
                "agent_id": agent_id,
                "audience": "frappe-controller",
                "operation_id": operation_id,
                "result": dict(result),
            },
            received_at=now,
        )

    return ingest
