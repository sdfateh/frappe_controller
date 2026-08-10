"""Frappe adapter for closed, bounded bulk inventory selectors."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .bulk import BulkContractError, BulkSelector
from .bulk_selection import BulkSelectionRepository, SelectableSite


def _rows_by_name(rows: Iterable[Any]) -> dict[str, Any]:
    return {row.name: row for row in rows}


def _string_set(raw: Any, name: str, *, maximum: int) -> frozenset[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        raise BulkContractError(f"invalid {name}") from None
    if (
        not isinstance(value, list) or len(value) > maximum
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise BulkContractError(f"invalid {name}")
    return frozenset(value)


class FrappeBulkSelectionRepository(BulkSelectionRepository):
    """Resolve only allowlisted inventory columns; never query caller-supplied text."""

    def __init__(self, frappe_module: Any | None = None, *, fetch_limit: int = 1001) -> None:
        if type(fetch_limit) is not int or not 2 <= fetch_limit <= 1001:
            raise ValueError("bulk selection fetch limit is invalid")
        if frappe_module is None:
            import frappe as frappe_module
        self.frappe = frappe_module
        self.fetch_limit = fetch_limit

    def candidates(self, selector: BulkSelector) -> tuple[SelectableSite, ...]:
        filters: dict[str, Any] = {"status": ["in", ["active", "maintenance"]]}
        if selector.exact_site_ids:
            filters["site_id"] = ["in", list(selector.exact_site_ids)]
        if selector.environment:
            filters["environment"] = selector.environment
        # Server Agent and Bench use their stable IDs as document names.
        if selector.agent_ids:
            filters["server_agent"] = ["in", list(selector.agent_ids)]
        if selector.bench_ids:
            filters["bench"] = ["in", list(selector.bench_ids)]

        sites = self.frappe.get_all(
            "Managed Site",
            filters=filters,
            fields=[
                "name", "site_id", "domain", "server_agent", "bench",
                "environment", "status", "labels_json",
            ],
            order_by="name asc",
            limit_page_length=self.fetch_limit,
        )
        if len(sites) >= self.fetch_limit:
            raise BulkContractError("bulk selector exceeds the bounded candidate scan")
        if not sites:
            return ()

        bench_names = sorted({row.bench for row in sites})
        agent_names = sorted({row.server_agent for row in sites})
        benches = _rows_by_name(self.frappe.get_all(
            "Bench",
            filters={"name": ["in", bench_names]},
            fields=["name", "bench_id", "server_agent", "environment", "enabled", "capabilities_json"],
            order_by="name asc",
            limit_page_length=self.fetch_limit,
        ))
        agents = _rows_by_name(self.frappe.get_all(
            "Server Agent",
            filters={"name": ["in", agent_names]},
            fields=["name", "agent_id", "environment", "enabled", "status", "inventory_digest", "capabilities_json"],
            order_by="name asc",
            limit_page_length=self.fetch_limit,
        ))

        result: list[SelectableSite] = []
        for site in sites:
            bench = benches.get(site.bench)
            agent = agents.get(site.server_agent)
            if (
                not bench or not agent or bench.server_agent != agent.name
                or site.environment != bench.environment or site.environment != agent.environment
            ):
                raise BulkContractError("bulk inventory ownership is inconsistent")
            labels = _string_set(site.labels_json, "site labels", maximum=32)
            if selector.labels and not set(selector.labels).issubset(labels):
                continue
            capabilities = (
                _string_set(agent.capabilities_json, "agent capabilities", maximum=256)
                & _string_set(bench.capabilities_json, "bench capabilities", maximum=256)
            )
            result.append(SelectableSite(
                server_agent=agent.name,
                agent_id=agent.agent_id,
                bench=bench.name,
                bench_id=bench.bench_id,
                managed_site=site.name,
                site_id=site.site_id,
                site_domain=site.domain,
                environment=site.environment,
                inventory_revision=agent.inventory_digest or "",
                labels=labels,
                capabilities=capabilities,
                agent_enabled=bool(agent.enabled) and agent.status != "Disabled",
                bench_enabled=bool(bench.enabled),
                site_enabled=site.status in {"active", "maintenance"},
            ))
        return tuple(result)


__all__ = ["FrappeBulkSelectionRepository"]
