"""Low-cardinality controller metric contract and Prometheus rendering."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping


OPERATION_STATES = (
    "awaiting_approval", "queued", "leased", "running", "succeeded", "failed",
    "cancelled", "timed_out", "needs_intervention", "dead_letter", "rejected",
)
BULK_STATES = (
    "draft", "previewed", "awaiting_approval", "approved", "canary", "running",
    "paused", "cancelling", "succeeded", "partial", "failed", "cancelled",
    "needs_intervention", "rejected",
)
AGENT_STATES = ("online", "offline", "disabled")


@dataclass(frozen=True, slots=True)
class ControllerMetrics:
    operation_counts: Mapping[str, int]
    bulk_counts: Mapping[str, int]
    agent_counts: Mapping[str, int]
    stale_inventory_count: int
    pending_operation_approvals: int
    pending_bulk_approvals: int
    certificates_expiring_7d: int
    certificates_expiring_30d: int
    oldest_queued_age_seconds: float
    oldest_bulk_approved_age_seconds: float

    def __post_init__(self) -> None:
        for supplied, expected, label in (
            (self.operation_counts, OPERATION_STATES, "operation"),
            (self.bulk_counts, BULK_STATES, "bulk"),
            (self.agent_counts, AGENT_STATES, "agent"),
        ):
            if set(supplied) != set(expected) or any(
                type(value) is not int or value < 0 for value in supplied.values()
            ):
                raise ValueError(f"controller {label} metrics are invalid")
        integer_values = (
            self.stale_inventory_count,
            self.pending_operation_approvals,
            self.pending_bulk_approvals,
            self.certificates_expiring_7d,
            self.certificates_expiring_30d,
        )
        if any(type(value) is not int or value < 0 for value in integer_values):
            raise ValueError("controller metric counts are invalid")
        for value in (
            self.oldest_queued_age_seconds,
            self.oldest_bulk_approved_age_seconds,
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("controller metric ages are invalid")
        if self.certificates_expiring_7d > self.certificates_expiring_30d:
            raise ValueError("certificate expiry windows are inconsistent")


def _number(value: int | float) -> str:
    return str(value) if type(value) is int else format(float(value), ".6f")


def render_controller_metrics(snapshot: ControllerMetrics) -> str:
    lines = [
        "# HELP frappe_controller_operations Operations by durable controller state.",
        "# TYPE frappe_controller_operations gauge",
    ]
    for state in OPERATION_STATES:
        lines.append(f'frappe_controller_operations{{state="{state}"}} {snapshot.operation_counts[state]}')
    lines.extend((
        "# HELP frappe_controller_bulk_operations Bulk parents by controller state.",
        "# TYPE frappe_controller_bulk_operations gauge",
    ))
    for state in BULK_STATES:
        lines.append(f'frappe_controller_bulk_operations{{state="{state}"}} {snapshot.bulk_counts[state]}')
    lines.extend((
        "# HELP frappe_controller_agents Agents by bounded health state.",
        "# TYPE frappe_controller_agents gauge",
    ))
    for state in AGENT_STATES:
        lines.append(f'frappe_controller_agents{{state="{state}"}} {snapshot.agent_counts[state]}')
    values = (
        ("frappe_controller_stale_inventory", snapshot.stale_inventory_count),
        ("frappe_controller_pending_operation_approvals", snapshot.pending_operation_approvals),
        ("frappe_controller_pending_bulk_approvals", snapshot.pending_bulk_approvals),
        ("frappe_controller_certificates_expiring_7d", snapshot.certificates_expiring_7d),
        ("frappe_controller_certificates_expiring_30d", snapshot.certificates_expiring_30d),
        ("frappe_controller_oldest_queued_age_seconds", snapshot.oldest_queued_age_seconds),
        ("frappe_controller_oldest_bulk_approved_age_seconds", snapshot.oldest_bulk_approved_age_seconds),
    )
    for name, value in values:
        lines.extend((f"# TYPE {name} gauge", f"{name} {_number(value)}"))
    return "\n".join(lines) + "\n"


__all__ = [
    "AGENT_STATES", "BULK_STATES", "ControllerMetrics", "OPERATION_STATES",
    "render_controller_metrics",
]
