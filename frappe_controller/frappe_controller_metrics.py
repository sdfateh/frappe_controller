"""Fixed-query Frappe adapter for controller metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .controller_metrics import (
    AGENT_STATES, BULK_STATES, OPERATION_STATES, ControllerMetrics,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _age(now: datetime, value: datetime | None) -> float:
    parsed = _aware(value)
    return 0.0 if parsed is None else max(0.0, (now - parsed).total_seconds())


def _grouped(frappe: Any, doctype: str, allowed: tuple[str, ...]) -> dict[str, int]:
    if doctype == "Operation":
        rows = frappe.db.sql(
            "SELECT state, COUNT(*) AS count FROM `tabOperation` GROUP BY state",
            as_dict=True,
        )
    elif doctype == "Bulk Operation":
        rows = frappe.db.sql(
            "SELECT state, COUNT(*) AS count FROM `tabBulk Operation` GROUP BY state",
            as_dict=True,
        )
    else:
        raise ValueError("unsupported controller metric group")
    result = {state: 0 for state in allowed}
    for row in rows:
        state = row["state"]
        if state not in result:
            raise ValueError(f"unknown {doctype} metric state")
        result[state] = int(row["count"])
    return result


def controller_metrics_snapshot(
    frappe: Any, *, now: datetime | None = None,
    offline_after_seconds: int = 180, inventory_stale_after_seconds: int = 600,
) -> ControllerMetrics:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if not 30 <= offline_after_seconds <= 86400 or not 60 <= inventory_stale_after_seconds <= 604800:
        raise ValueError("controller metric freshness policy is invalid")
    agents = frappe.get_all(
        "Server Agent",
        fields=["enabled", "last_seen", "inventory_updated_at"],
        limit_page_length=10001,
    )
    if len(agents) >= 10001:
        raise ValueError("controller agent metric scan exceeds its bound")
    agent_counts = {state: 0 for state in AGENT_STATES}
    stale_inventory = 0
    offline_cutoff = current - timedelta(seconds=offline_after_seconds)
    inventory_cutoff = current - timedelta(seconds=inventory_stale_after_seconds)
    for agent in agents:
        if not bool(agent.enabled):
            agent_counts["disabled"] += 1
            continue
        seen = _aware(agent.last_seen)
        if seen is None or seen < offline_cutoff:
            agent_counts["offline"] += 1
        else:
            agent_counts["online"] += 1
        inventory = _aware(agent.inventory_updated_at)
        if inventory is None or inventory < inventory_cutoff:
            stale_inventory += 1

    certificates = frappe.get_all(
        "Agent Certificate", filters={"status": "active"}, fields=["valid_until"],
        limit_page_length=10001,
    )
    if len(certificates) >= 10001:
        raise ValueError("controller certificate metric scan exceeds its bound")
    expires_7d = sum(
        current <= (_aware(row.valid_until) or current - timedelta(seconds=1)) <= current + timedelta(days=7)
        for row in certificates
    )
    expires_30d = sum(
        current <= (_aware(row.valid_until) or current - timedelta(seconds=1)) <= current + timedelta(days=30)
        for row in certificates
    )
    oldest_queue = frappe.db.get_value(
        "Operation", {"state": "queued"}, "MIN(creation)"
    )
    oldest_bulk = frappe.db.get_value(
        "Bulk Operation", {"state": "approved"}, "MIN(approved_at)"
    )
    return ControllerMetrics(
        operation_counts=_grouped(frappe, "Operation", OPERATION_STATES),
        bulk_counts=_grouped(frappe, "Bulk Operation", BULK_STATES),
        agent_counts=agent_counts,
        stale_inventory_count=stale_inventory,
        pending_operation_approvals=int(frappe.db.count("Operation", {"approval_status": "pending"})),
        pending_bulk_approvals=int(frappe.db.count("Bulk Operation", {"approval_status": "pending"})),
        certificates_expiring_7d=expires_7d,
        certificates_expiring_30d=expires_30d,
        oldest_queued_age_seconds=_age(current, oldest_queue),
        oldest_bulk_approved_age_seconds=_age(current, oldest_bulk),
    )


__all__ = ["controller_metrics_snapshot"]
