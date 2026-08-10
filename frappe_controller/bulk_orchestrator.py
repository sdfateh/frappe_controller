"""Idempotent controller-side fan-out for immutable bulk target snapshots."""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, Sequence

from .bulk import (
    BulkPlan,
    BulkTargetSnapshot,
    aggregate_child_states,
    failure_budget_exhausted,
)


_FAILURE_STATES = frozenset(
    {"failed", "timed_out", "dead_letter", "needs_intervention", "rejected"}
)
_TERMINAL_STATES = _FAILURE_STATES | frozenset({"succeeded", "cancelled"})


class BulkOrchestrationError(RuntimeError):
    """Bulk state is corrupt or cannot progress safely."""


@dataclass(frozen=True, slots=True)
class BulkParentRuntime:
    parent_id: str
    plan: BulkPlan
    state: str
    pause_requested: bool = False
    cancel_requested: bool = False


@dataclass(frozen=True, slots=True)
class BulkTargetRuntime:
    snapshot: BulkTargetSnapshot
    state: str = "planned"
    child_operation_id: str | None = None
    attempt: int = 0
    cancellation_sent: bool = False


@dataclass(frozen=True, slots=True)
class InflightCounts:
    global_count: int
    by_agent: dict[str, int]
    by_bench: dict[tuple[str, str], int]


class BulkRepository(Protocol):
    def lock_parent(self, parent_id: str) -> AbstractContextManager[BulkParentRuntime]: ...
    def targets(self, parent_id: str) -> Sequence[BulkTargetRuntime]: ...
    def inflight_counts(self) -> InflightCounts: ...
    def can_start(self, target: BulkTargetRuntime) -> bool: ...
    def create_child(
        self,
        parent_id: str,
        target: BulkTargetRuntime,
        child_operation_id: str,
        idempotency_key: str,
    ) -> None: ...
    def request_child_cancel(self, parent_id: str, target: BulkTargetRuntime) -> None: ...
    def mark_unstarted_cancelled(self, parent_id: str, target: BulkTargetRuntime) -> None: ...
    def update_parent(self, parent_id: str, state: str, aggregate: object) -> None: ...


def _child_identity(parent_id: str, target_key: str, attempt: int) -> tuple[str, str]:
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"frappe-controller:bulk:{parent_id}")
    child = str(uuid.uuid5(namespace, f"{target_key}:{attempt}"))
    return child, f"bulk:{parent_id}:{target_key}:{attempt}:{child}"


class BulkOrchestrator:
    def __init__(self, repository: BulkRepository) -> None:
        self.repository = repository

    def reconcile(self, parent_id: str) -> str:
        with self.repository.lock_parent(parent_id) as parent:
            targets = tuple(self.repository.targets(parent_id))
            if len(targets) != len(parent.plan.targets):
                raise BulkOrchestrationError("bulk target snapshot count changed")
            for persisted, planned in zip(targets, parent.plan.targets, strict=True):
                if persisted.snapshot != planned:
                    raise BulkOrchestrationError("bulk target snapshot changed")
            if parent.state in {"succeeded", "partial", "cancelled", "failed"}:
                return parent.state

            if parent.cancel_requested:
                for target in targets:
                    if target.state == "planned":
                        self.repository.mark_unstarted_cancelled(parent_id, target)
                    elif target.state not in _TERMINAL_STATES and not target.cancellation_sent:
                        self.repository.request_child_cancel(parent_id, target)
                return self._update(parent_id, "cancelling")

            if parent.pause_requested:
                return self._update(parent_id, "paused")

            canary_size = min(parent.plan.policy.canary_size, len(targets))
            canaries = targets[:canary_size]
            canary_terminal = all(target.state in _TERMINAL_STATES for target in canaries)
            if canary_terminal and failure_budget_exhausted(
                (target.state for target in canaries), parent.plan.policy
            ):
                return self._update(parent_id, "paused")
            eligible = targets if canary_terminal else canaries

            counts = self.repository.inflight_counts()
            global_count = counts.global_count
            by_agent = dict(counts.by_agent)
            by_bench = dict(counts.by_bench)
            for target in eligible:
                if target.state != "planned":
                    continue
                if not self.repository.can_start(target):
                    continue
                agent = target.snapshot.agent_id
                bench = (agent, target.snapshot.bench_id)
                policy = parent.plan.policy
                if global_count >= policy.global_concurrency:
                    break
                if by_agent.get(agent, 0) >= policy.per_agent_concurrency:
                    continue
                if by_bench.get(bench, 0) >= policy.per_bench_concurrency:
                    continue
                child_id, idempotency_key = _child_identity(
                    parent_id, target.snapshot.target_key, target.attempt
                )
                self.repository.create_child(
                    parent_id, target, child_id, idempotency_key
                )
                global_count += 1
                by_agent[agent] = by_agent.get(agent, 0) + 1
                by_bench[bench] = by_bench.get(bench, 0) + 1

            refreshed = tuple(self.repository.targets(parent_id))
            if failure_budget_exhausted(
                (target.state for target in refreshed), parent.plan.policy
            ):
                return self._update(parent_id, "paused")
            return self._update(parent_id, "running" if canary_terminal else "canary")

    def retry_failed(self, parent_id: str) -> int:
        raise BulkOrchestrationError(
            "failed targets require a new approval-bound parent with fresh previews"
        )

    def _update(self, parent_id: str, active_state: str) -> str:
        targets = tuple(self.repository.targets(parent_id))
        aggregate = aggregate_child_states(target.state for target in targets)
        if all(target.state in _TERMINAL_STATES for target in targets):
            if aggregate.succeeded == aggregate.total:
                state = "succeeded"
            elif aggregate.cancelled == aggregate.total:
                state = "cancelled"
            else:
                state = "partial"
        else:
            state = active_state
        self.repository.update_parent(parent_id, state, aggregate)
        return state
