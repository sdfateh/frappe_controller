"""Safe deterministic resolution of closed bulk selectors into snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .bulk import BulkContractError, BulkSelector, BulkTargetSnapshot


@dataclass(frozen=True, slots=True)
class SelectableSite:
    server_agent: str
    agent_id: str
    bench: str
    bench_id: str
    managed_site: str
    site_id: str
    site_domain: str
    environment: str
    inventory_revision: str
    labels: frozenset[str]
    capabilities: frozenset[str]
    agent_enabled: bool = True
    bench_enabled: bool = True
    site_enabled: bool = True


class BulkSelectionRepository(Protocol):
    def candidates(self, selector: BulkSelector) -> Sequence[SelectableSite]: ...


@dataclass(frozen=True, slots=True)
class ResolvedBulkSelection:
    selector: BulkSelector
    targets: tuple[BulkTargetSnapshot, ...]
    environment_counts: tuple[tuple[str, int], ...]
    selected_sites: tuple[SelectableSite, ...]


class BulkSelectionService:
    """Validate repository results rather than trusting its query implementation."""

    def __init__(self, repository: BulkSelectionRepository, *, system_maximum: int = 1000):
        if type(system_maximum) is not int or not 1 <= system_maximum <= 1000:
            raise ValueError("invalid bulk system maximum")
        self.repository = repository
        self.system_maximum = system_maximum

    def resolve(
        self,
        selector: BulkSelector,
        *,
        operation_type: str,
        policy_maximum: int,
        allow_mixed_environments: bool = False,
    ) -> ResolvedBulkSelection:
        if type(policy_maximum) is not int or not 1 <= policy_maximum <= self.system_maximum:
            raise BulkContractError("invalid bulk policy maximum")
        rows = tuple(self.repository.candidates(selector))
        if not rows:
            raise BulkContractError("bulk selector resolved no targets")
        if len(rows) > policy_maximum or len(rows) > self.system_maximum:
            raise BulkContractError("bulk target count exceeds policy")

        by_site: dict[str, SelectableSite] = {}
        for row in rows:
            if not isinstance(row, SelectableSite):
                raise BulkContractError("bulk repository returned an invalid target")
            if row.site_id in by_site:
                raise BulkContractError("bulk repository returned duplicate sites")
            by_site[row.site_id] = row
            if not (row.agent_enabled and row.bench_enabled and row.site_enabled):
                raise BulkContractError("bulk target is disabled")
            if operation_type not in row.capabilities:
                raise BulkContractError("bulk target lacks required capability")
            if selector.exact_site_ids and row.site_id not in selector.exact_site_ids:
                raise BulkContractError("bulk target escapes exact-site selector")
            if selector.environment and row.environment != selector.environment:
                raise BulkContractError("bulk target escapes environment selector")
            if selector.agent_ids and row.agent_id not in selector.agent_ids:
                raise BulkContractError("bulk target escapes agent selector")
            if selector.bench_ids and row.bench_id not in selector.bench_ids:
                raise BulkContractError("bulk target escapes bench selector")
            if selector.labels and not set(selector.labels).issubset(row.labels):
                raise BulkContractError("bulk target escapes label selector")

        if selector.exact_site_ids and set(by_site) != set(selector.exact_site_ids):
            raise BulkContractError("one or more exact sites are unknown or disabled")
        environments = {row.environment for row in rows}
        if selector.environment == "staging" and "production" in environments:
            raise BulkContractError("staging selector included production")
        if len(environments) > 1 and not allow_mixed_environments:
            raise BulkContractError("mixed-environment bulk selection is not authorized")

        provisional = [
            BulkTargetSnapshot(
                ordinal=0,
                agent_id=row.agent_id,
                bench_id=row.bench_id,
                site_id=row.site_id,
                site_domain=row.site_domain,
                environment=row.environment,
                inventory_revision=row.inventory_revision,
            )
            for row in rows
        ]
        ordered = sorted(provisional, key=lambda target: target.target_key)
        targets = tuple(
            BulkTargetSnapshot(
                ordinal=index,
                agent_id=target.agent_id,
                bench_id=target.bench_id,
                site_id=target.site_id,
                site_domain=target.site_domain,
                environment=target.environment,
                inventory_revision=target.inventory_revision,
            )
            for index, target in enumerate(ordered)
        )
        counts = tuple(
            (environment, sum(row.environment == environment for row in rows))
            for environment in sorted(environments)
        )
        ordered_sites = tuple(by_site[target.site_id] for target in targets)
        return ResolvedBulkSelection(selector, targets, counts, ordered_sites)


__all__ = [
    "BulkSelectionRepository",
    "BulkSelectionService",
    "ResolvedBulkSelection",
    "SelectableSite",
]
