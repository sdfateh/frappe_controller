"""Dependency-free contracts for safe controller-side bulk orchestration.

Bulk parents never become agent commands.  They snapshot targets and create one
ordinary, immutable single-site child operation for each target.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Sequence


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_ENVIRONMENTS = frozenset({"development", "staging", "production"})
_FAILURE_STATES = frozenset(
    {"failed", "timed_out", "dead_letter", "needs_intervention", "rejected"}
)
_TERMINAL_STATES = _FAILURE_STATES | frozenset({"succeeded", "cancelled"})


class BulkContractError(ValueError):
    """A selector, immutable snapshot, or orchestration policy is unsafe."""


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise BulkContractError(f"invalid {name}")
    return value


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise BulkContractError(f"invalid {name}")
    return value


def _unique(values: Sequence[str], name: str, *, maximum: int = 1000) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or len(values) > maximum:
        raise BulkContractError(f"invalid {name}")
    normalized = tuple(_identifier(value, name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise BulkContractError(f"duplicate {name}")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class BulkSelector:
    exact_site_ids: tuple[str, ...] = ()
    environment: str | None = None
    agent_ids: tuple[str, ...] = ()
    bench_ids: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "exact_site_ids", _unique(self.exact_site_ids, "site id"))
        object.__setattr__(self, "agent_ids", _unique(self.agent_ids, "agent id", maximum=128))
        object.__setattr__(self, "bench_ids", _unique(self.bench_ids, "bench id", maximum=128))
        object.__setattr__(self, "labels", _unique(self.labels, "label", maximum=32))
        if self.environment is not None and self.environment not in _ENVIRONMENTS:
            raise BulkContractError("invalid environment")
        if not any(
            (
                self.exact_site_ids,
                self.environment,
                self.agent_ids,
                self.bench_ids,
                self.labels,
            )
        ):
            raise BulkContractError("bulk selector cannot match the entire fleet")


@dataclass(frozen=True, slots=True)
class BulkTargetSnapshot:
    ordinal: int
    agent_id: str
    bench_id: str
    site_id: str
    site_domain: str
    environment: str
    inventory_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise BulkContractError("invalid target ordinal")
        for name in ("agent_id", "bench_id", "site_id"):
            _identifier(getattr(self, name), name)
        if (
            not isinstance(self.site_domain, str)
            or self.site_domain != self.site_domain.lower()
            or not _DOMAIN_RE.fullmatch(self.site_domain)
        ):
            raise BulkContractError("invalid site domain")
        if self.environment not in _ENVIRONMENTS:
            raise BulkContractError("invalid target environment")
        _digest(self.inventory_revision, "inventory revision")

    @property
    def target_key(self) -> str:
        return "|".join(
            f"{len(value.encode('utf-8'))}:{value}"
            for value in (self.agent_id, self.bench_id, self.site_id)
        )


@dataclass(frozen=True, slots=True)
class BulkExecutionPolicy:
    maximum_targets: int
    canary_size: int = 1
    global_concurrency: int = 10
    per_agent_concurrency: int = 2
    per_bench_concurrency: int = 1
    maximum_failures: int = 1
    maximum_failure_ratio: float = 0.1

    def __post_init__(self) -> None:
        integer_bounds = {
            "maximum_targets": (self.maximum_targets, 1, 1000),
            "canary_size": (self.canary_size, 1, 100),
            "global_concurrency": (self.global_concurrency, 1, 256),
            "per_agent_concurrency": (self.per_agent_concurrency, 1, 64),
            "per_bench_concurrency": (self.per_bench_concurrency, 1, 32),
            "maximum_failures": (self.maximum_failures, 0, 1000),
        }
        for name, (value, minimum, maximum) in integer_bounds.items():
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise BulkContractError(f"invalid {name}")
        if self.canary_size > self.maximum_targets:
            raise BulkContractError("canary exceeds maximum targets")
        if self.per_agent_concurrency > self.global_concurrency:
            raise BulkContractError("agent concurrency exceeds global concurrency")
        if self.per_bench_concurrency > self.per_agent_concurrency:
            raise BulkContractError("bench concurrency exceeds agent concurrency")
        if (
            not isinstance(self.maximum_failure_ratio, float)
            or not math.isfinite(self.maximum_failure_ratio)
            or not 0 <= self.maximum_failure_ratio <= 1
        ):
            raise BulkContractError("invalid maximum failure ratio")


@dataclass(frozen=True, slots=True)
class BulkPlan:
    parent_operation_id: str
    operation_type: str
    payload_hash: str
    selector: BulkSelector
    targets: tuple[BulkTargetSnapshot, ...]
    dry_run_hash: str
    policy: BulkExecutionPolicy
    retry_of: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.parent_operation_id, "parent operation id")
        _identifier(self.operation_type, "operation type")
        _digest(self.payload_hash, "payload hash")
        _digest(self.dry_run_hash, "dry-run hash")
        if self.retry_of is not None:
            _identifier(self.retry_of, "retry parent")
        if not isinstance(self.targets, tuple) or not self.targets:
            raise BulkContractError("bulk plan requires targets")
        if len(self.targets) > self.policy.maximum_targets:
            raise BulkContractError("bulk target count exceeds policy")
        expected = tuple(range(len(self.targets)))
        if tuple(target.ordinal for target in self.targets) != expected:
            raise BulkContractError("bulk targets must use deterministic contiguous order")
        keys = tuple(target.target_key for target in self.targets)
        if len(set(keys)) != len(keys):
            raise BulkContractError("bulk target snapshot contains duplicates")
        if keys != tuple(sorted(keys)):
            raise BulkContractError("bulk target snapshot is not deterministically sorted")
        if self.selector.environment is not None and any(
            target.environment != self.selector.environment for target in self.targets
        ):
            raise BulkContractError("target environment escapes selector")
        if self.selector.environment == "staging" and any(
            target.environment == "production" for target in self.targets
        ):
            raise BulkContractError("staging selector cannot include production")
        if self.selector.exact_site_ids and any(
            target.site_id not in self.selector.exact_site_ids for target in self.targets
        ):
            raise BulkContractError("target escapes exact-site selector")

    @property
    def snapshot_hash(self) -> str:
        return canonical_bulk_hash(self)

    def waves(self) -> tuple[tuple[BulkTargetSnapshot, ...], ...]:
        canary_count = min(self.policy.canary_size, len(self.targets))
        canary = self.targets[:canary_count]
        remaining = self.targets[canary_count:]
        return (canary,) if not remaining else (canary, remaining)


def canonical_bulk_hash(plan: BulkPlan) -> str:
    value = {
        "version": 1,
        "parent_operation_id": plan.parent_operation_id,
        "operation_type": plan.operation_type,
        "payload_hash": plan.payload_hash,
        "dry_run_hash": plan.dry_run_hash,
        "retry_of": plan.retry_of,
        "selector": asdict(plan.selector),
        "targets": [asdict(target) for target in plan.targets],
        "policy": asdict(plan.policy),
    }
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BulkAggregate:
    total: int
    planned: int
    queued: int
    running: int
    succeeded: int
    failed: int
    cancelled: int
    needs_intervention: int

    def __post_init__(self) -> None:
        values = (
            self.total,
            self.planned,
            self.queued,
            self.running,
            self.succeeded,
            self.failed,
            self.cancelled,
            self.needs_intervention,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
            raise BulkContractError("bulk aggregate counts must be non-negative integers")
        if sum(values[1:]) != self.total:
            raise BulkContractError("bulk aggregate buckets must equal total targets")


def aggregate_child_states(states: Iterable[str]) -> BulkAggregate:
    counts = {
        "planned": 0,
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
        "needs_intervention": 0,
    }
    total = 0
    for state in states:
        total += 1
        if state in {"timed_out", "dead_letter", "rejected"}:
            counts["failed"] += 1
        elif state == "awaiting_approval":
            counts["planned"] += 1
        elif state == "leased":
            counts["running"] += 1
        elif state in counts:
            counts[state] += 1
        else:
            raise BulkContractError("unknown child state")
    return BulkAggregate(total=total, **counts)


def failure_budget_exhausted(
    states: Iterable[str], policy: BulkExecutionPolicy
) -> bool:
    observed = tuple(states)
    failures = sum(state in _FAILURE_STATES for state in observed)
    terminal = sum(state in _TERMINAL_STATES for state in observed)
    ratio = failures / terminal if terminal else 0.0
    count_exhausted = failures > 0 and (
        policy.maximum_failures == 0 or failures >= policy.maximum_failures
    )
    ratio_exhausted = failures > 0 and ratio >= policy.maximum_failure_ratio
    return count_exhausted or ratio_exhausted
