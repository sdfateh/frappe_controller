"""Transactional Frappe repositories for inventory and agent ingestion.

These adapters deliberately do not commit.  Frappe owns the request transaction;
the row locks and compare-and-set checks below therefore cover the complete route.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Mapping

try:  # Support both an installed Frappe app and dependency-free unit imports.
    from .ingestion import (
        IngestedEvent,
        IngestedResult,
        IngestionConflictError,
        IngestionOwnershipError,
        OperationIdentity,
        StoredEvent,
        StoredResult,
    )
    from .inventory import (
        AgentProjection,
        HeartbeatInventory,
        InventoryConflictError,
        InventoryOwnershipError,
        StoredInventory,
        parse_heartbeat_state,
    )
except ImportError:  # pragma: no cover - used by the standalone controller tests
    from ingestion import (  # type: ignore
        IngestedEvent,
        IngestedResult,
        IngestionConflictError,
        IngestionOwnershipError,
        OperationIdentity,
        StoredEvent,
        StoredResult,
    )
    from inventory import (  # type: ignore
        AgentProjection,
        HeartbeatInventory,
        InventoryConflictError,
        InventoryOwnershipError,
        StoredInventory,
        parse_heartbeat_state,
    )


def _runtime() -> Any:
    import frappe

    return frappe


def _row(rows: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return rows[0] if rows else None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load_snapshot(encoded: str, digest: str) -> StoredInventory:
    raw = json.loads(encoded)
    heartbeat = parse_heartbeat_state(
        {
            "status": "ready",
            "version": "stored",
            "inventory_digest": digest,
            "inventory": raw,
        }
    )
    return StoredInventory(digest, heartbeat.inventory.benches, 0)


class FrappeInventoryRepository:
    """Authoritative inventory replacement using a locked Server Agent row."""

    def __init__(self, frappe_module: Any | None = None) -> None:
        self.frappe = frappe_module or _runtime()
        self.db = self.frappe.db

    def _agent(self, agent_id: str, *, lock: bool = False) -> Mapping[str, Any] | None:
        suffix = " FOR UPDATE" if lock else ""
        return _row(
            self.db.sql(
                "SELECT name, agent_id, environment, enabled, reported_status, "
                "agent_version, capabilities_json, last_seen, inventory_digest, "
                "inventory_snapshot_json, inventory_revision FROM `tabServer Agent` "
                f"WHERE agent_id=%s{suffix}",
                (agent_id,),
                as_dict=True,
            )
        )

    def current_inventory(self, agent_id: str) -> StoredInventory | None:
        row = self._agent(agent_id)
        if not row or not row.get("inventory_digest") or not row.get("inventory_snapshot_json"):
            return None
        stored = _load_snapshot(row["inventory_snapshot_json"], row["inventory_digest"])
        return StoredInventory(stored.digest, stored.benches, int(row.get("inventory_revision") or 0))

    def bench_owner(self, bench_id: str) -> str | None:
        row = _row(
            self.db.sql(
                "SELECT a.agent_id FROM `tabBench` b JOIN `tabServer Agent` a "
                "ON a.name=b.server_agent WHERE b.bench_id=%s",
                (bench_id,),
                as_dict=True,
            )
        )
        return None if row is None else str(row["agent_id"])

    def site_owner(self, domain: str) -> tuple[str, str] | None:
        row = _row(
            self.db.sql(
                "SELECT a.agent_id, b.bench_id FROM `tabManaged Site` s "
                "JOIN `tabServer Agent` a ON a.name=s.server_agent "
                "JOIN `tabBench` b ON b.name=s.bench WHERE s.domain=%s",
                (domain,),
                as_dict=True,
            )
        )
        return None if row is None else (str(row["agent_id"]), str(row["bench_id"]))

    def replace_inventory(
        self,
        agent_id: str,
        heartbeat: HeartbeatInventory,
        *,
        previous_digest: str | None,
        revision: int,
        observed_at: datetime,
    ) -> None:
        # Frappe/MariaDB datetime columns store naive UTC values.
        observed_at = observed_at.astimezone(UTC).replace(tzinfo=None)
        agent = self._agent(agent_id, lock=True)
        if agent is None or not agent.get("enabled"):
            raise InventoryOwnershipError("unknown or disabled agent")
        actual = agent.get("inventory_digest") or None
        if actual != previous_digest or int(agent.get("inventory_revision") or 0) + 1 != revision:
            raise InventoryConflictError("inventory compare-and-set conflict")

        current_benches = {item.bench_id: item for item in heartbeat.inventory.benches}
        current_sites = {
            site.domain: (bench.bench_id, site)
            for bench in heartbeat.inventory.benches
            for site in bench.sites
        }
        for bench_id in current_benches:
            owner = self.bench_owner(bench_id)
            if owner is not None and owner != agent_id:
                raise InventoryOwnershipError("bench owned by another agent")
        for domain, (bench_id, _) in current_sites.items():
            owner = self.site_owner(domain)
            if owner is not None and owner != (agent_id, bench_id):
                raise InventoryOwnershipError("site owned by another agent or bench")

        old = self.current_inventory(agent_id)
        old_benches = set() if old is None else {item.bench_id for item in old.benches}
        old_sites = set() if old is None else {
            site.domain for bench in old.benches for site in bench.sites
        }
        for removed in sorted(old_benches - set(current_benches)):
            self.db.set_value(
                "Bench", removed, {"enabled": 0, "health_status": "unreachable"},
                update_modified=False,
            )
        for removed in sorted(old_sites - set(current_sites)):
            self.db.set_value(
                "Managed Site", removed, {"status": "missing", "health_status": "unreachable"},
                update_modified=False,
            )

        environment = agent["environment"]
        for bench in heartbeat.inventory.benches:
            versions = dict(bench.versions)
            values = {
                "server_agent": agent["name"],
                "display_name": bench.bench_id,
                "environment": environment,
                "enabled": 1,
                "frappe_version": versions.get("frappe"),
                "erpnext_version": versions.get("erpnext"),
                "installed_apps_json": _json(list(bench.versions)),
                "capabilities_json": _json(list(bench.capabilities)),
                "health_status": "healthy" if heartbeat.status == "ready" else "degraded",
                "inventory_updated_at": observed_at,
            }
            if self.db.exists("Bench", bench.bench_id):
                self.db.set_value("Bench", bench.bench_id, values, update_modified=False)
            else:
                self.frappe.get_doc({"doctype": "Bench", "bench_id": bench.bench_id, **values}).insert(ignore_permissions=True)
            for site in bench.sites:
                site_values = {
                    "domain": site.domain,
                    "server_agent": agent["name"],
                    "bench": bench.bench_id,
                    "environment": environment,
                    "status": "maintenance" if site.maintenance_mode else "active",
                    "installed_apps_json": _json(list(site.apps)),
                    "database_name": site.database_name,
                    "scheduler_enabled": bool(site.scheduler_enabled),
                    "maintenance_mode": bool(site.maintenance_mode),
                    "health_status": "healthy" if heartbeat.status == "ready" else "degraded",
                    "inventory_updated_at": observed_at,
                }
                if self.db.exists("Managed Site", site.domain):
                    self.db.set_value("Managed Site", site.domain, site_values, update_modified=False)
                else:
                    self.frappe.get_doc(
                        {"doctype": "Managed Site", "site_id": site.domain, **site_values}
                    ).insert(ignore_permissions=True)

        capabilities = sorted({item for bench in heartbeat.inventory.benches for item in bench.capabilities})
        self.db.set_value(
            "Server Agent",
            agent["name"],
            {
                "status": "Online",
                "reported_status": heartbeat.status,
                "agent_version": heartbeat.version,
                "capabilities_json": _json(capabilities),
                "last_seen": observed_at,
                "inventory_updated_at": observed_at,
                "inventory_revision": revision,
                "inventory_digest": heartbeat.inventory_digest,
                "inventory_snapshot_json": heartbeat.inventory.canonical_json(),
            },
            update_modified=False,
        )

    def agent_projection(self, agent_id: str) -> AgentProjection | None:
        row = self._agent(agent_id)
        if row is None or not row.get("last_seen") or not row.get("inventory_digest"):
            return None
        capabilities = tuple(json.loads(row.get("capabilities_json") or "[]"))
        reported = row.get("reported_status") or "error"
        return AgentProjection(
            agent_id,
            reported,
            reported,
            row.get("agent_version") or "unknown",
            _aware(row["last_seen"]),
            row["inventory_digest"],
            int(row.get("inventory_revision") or 0),
            capabilities,
            True,
        )


class FrappeIngestionRepository:
    """Append-only event and monotonic result adapter with row-level fencing."""

    def __init__(self, frappe_module: Any | None = None) -> None:
        self.frappe = frappe_module or _runtime()
        self.db = self.frappe.db

    def _operation_row(self, operation_id: str, *, lock: bool = False) -> Mapping[str, Any] | None:
        suffix = " FOR UPDATE" if lock else ""
        return _row(
            self.db.sql(
                "SELECT o.name, o.operation_id, a.agent_id, b.bench_id, s.domain AS site_domain, "
                "o.operation_type, o.state, o.last_event_sequence, o.result_hash, o.result_json, "
                "o.bulk_target "
                "FROM `tabOperation` o JOIN `tabServer Agent` a ON a.name=o.server_agent "
                "JOIN `tabBench` b ON b.name=o.bench LEFT JOIN `tabManaged Site` s ON s.name=o.managed_site "
                f"WHERE o.operation_id=%s{suffix}",
                (operation_id,),
                as_dict=True,
            )
        )

    def operation(self, operation_id: str) -> OperationIdentity | None:
        row = self._operation_row(operation_id)
        if row is None:
            return None
        return OperationIdentity(
            row["operation_id"], row["agent_id"], row["bench_id"], row.get("site_domain"),
            row["operation_type"], row["state"], int(row.get("last_event_sequence") or 0),
        )

    def event(self, operation_id: str, sequence: int) -> StoredEvent | None:
        row = _row(
            self.db.sql(
                "SELECT e.operation_id, e.sequence, e.body_hash FROM `tabOperation Event` e "
                "WHERE e.operation_id=%s AND e.sequence=%s",
                (operation_id, sequence),
                as_dict=True,
            )
        )
        return None if row is None else StoredEvent(row["operation_id"], int(row["sequence"]), row["body_hash"])

    def append_events(
        self,
        agent_id: str,
        operation_id: str,
        *,
        expected_last_sequence: int,
        events: tuple[IngestedEvent, ...],
    ) -> int:
        operation = self._operation_row(operation_id, lock=True)
        if operation is None or operation["agent_id"] != agent_id:
            raise IngestionOwnershipError("operation belongs to another agent")
        cursor = int(operation.get("last_event_sequence") or 0)
        if cursor != expected_last_sequence:
            if events and cursor == events[-1].sequence:
                replay = self.db.sql(
                    "SELECT sequence, body_hash FROM `tabOperation Event` "
                    "WHERE operation=%s AND sequence BETWEEN %s AND %s ORDER BY sequence FOR UPDATE",
                    (operation["name"], events[0].sequence, events[-1].sequence),
                    as_dict=True,
                )
                expected = [(item.sequence, item.body_hash) for item in events]
                actual = [(int(item["sequence"]), item["body_hash"]) for item in replay]
                if actual == expected:
                    return cursor
            raise IngestionConflictError("event cursor compare-and-set conflict")
        for event in events:
            if event.sequence != cursor + 1:
                raise IngestionConflictError("event gap")
            self.frappe.get_doc(
                {
                    "doctype": "Operation Event",
                    "operation": operation["name"],
                    "operation_id": operation_id,
                    "sequence": event.sequence,
                    "attempt": event.attempt,
                    "step": event.step,
                    "kind": event.kind,
                    "details_json": event.details_json,
                    "body_hash": event.body_hash,
                    "agent_created_at": _aware(event.agent_created_at).replace(tzinfo=None),
                    "received_at": _aware(event.received_at).replace(tzinfo=None),
                }
            ).insert(ignore_permissions=True)
            cursor = event.sequence
        current = self.db.get_value("Operation", operation["name"], "last_event_sequence")
        if int(current or 0) != cursor:
            raise IngestionConflictError("event cursor commit conflict")
        return cursor

    def result(self, operation_id: str) -> StoredResult | None:
        row = self._operation_row(operation_id)
        if row is None or not row.get("result_hash"):
            return None
        return StoredResult(operation_id, row["state"], row["result_hash"], row.get("result_json") or "null")

    def commit_result(
        self,
        agent_id: str,
        operation_id: str,
        *,
        expected_previous_hash: str | None,
        result: IngestedResult,
    ) -> None:
        operation = self._operation_row(operation_id, lock=True)
        if operation is None or operation["agent_id"] != agent_id:
            raise IngestionOwnershipError("operation belongs to another agent")
        actual = operation.get("result_hash") or None
        if actual != expected_previous_hash:
            if actual == result.body_hash:
                return
            raise IngestionConflictError("result compare-and-set conflict")
        state = result.status
        values: dict[str, Any] = {
            "state": state,
            "result_json": result.result_json,
            "result_hash": result.body_hash,
            "error_code": result.error_code,
            "lease_expires_at": None,
        }
        if state == "running" and operation["state"] != "running":
            values["started_at"] = _aware(result.received_at).replace(tzinfo=None)
        if state in {"succeeded", "failed", "cancelled", "timed_out", "needs_intervention", "dead_letter", "rejected"}:
            values["completed_at"] = _aware(result.received_at).replace(tzinfo=None)
        self.db.set_value("Operation", operation["name"], values, update_modified=False)
        self.db.set_value(
            "Operation Target",
            {"operation": operation["name"]},
            {"state": state, "result_json": result.result_json, "error_code": result.error_code},
            update_modified=False,
        )
        if operation.get("bulk_target"):
            bulk_values: dict[str, Any] = {
                "state": state,
                "result_json": result.result_json,
                "error_code": result.error_code,
            }
            if state == "running":
                bulk_values["started_at"] = _aware(result.received_at).replace(tzinfo=None)
            if state in {
                "succeeded", "failed", "cancelled", "timed_out",
                "needs_intervention", "dead_letter", "rejected",
            }:
                bulk_values["completed_at"] = _aware(result.received_at).replace(tzinfo=None)
            self.db.set_value(
                "Bulk Operation Target", operation["bulk_target"], bulk_values,
                update_modified=False,
            )
