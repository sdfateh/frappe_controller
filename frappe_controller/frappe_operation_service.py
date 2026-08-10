"""Frappe repository for server-authored immutable single-target operations."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .operation_service import (
    ApprovalRule,
    AuthoredOperation,
    OperationAuthoringError,
    OperationAuthoringRepository,
    TargetSnapshot,
)


def _json_string_set(raw: Any, field: str) -> frozenset[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        raise OperationAuthoringError(f"target {field} is invalid") from None
    if (
        not isinstance(value, list) or len(value) > 256
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise OperationAuthoringError(f"target {field} is invalid")
    return frozenset(value)


class FrappeOperationAuthoringRepository(OperationAuthoringRepository):
    def __init__(self, frappe_module: Any | None = None) -> None:
        if frappe_module is None:
            import frappe as frappe_module
        self.frappe = frappe_module
        self.db = frappe_module.db

    def resolve_target(
        self, server_agent: str, bench: str, managed_site: str | None
    ) -> TargetSnapshot:
        agent = self.db.get_value(
            "Server Agent", server_agent,
            ["name", "agent_id", "environment", "enabled", "status", "inventory_digest", "capabilities_json"],
            as_dict=True,
        )
        bench_row = self.db.get_value(
            "Bench", bench,
            ["name", "bench_id", "server_agent", "environment", "enabled", "capabilities_json"],
            as_dict=True,
        )
        if not agent or not bench_row or bench_row.server_agent != server_agent:
            raise OperationAuthoringError("unknown operation target")
        if bench_row.environment != agent.environment:
            raise OperationAuthoringError("target environment ownership is inconsistent")
        agent_caps = _json_string_set(agent.capabilities_json, "agent capabilities")
        bench_caps = _json_string_set(bench_row.capabilities_json, "bench capabilities")

        site_name = None
        site_domain = None
        site_active = True
        if managed_site is not None:
            site = self.db.get_value(
                "Managed Site", managed_site,
                ["name", "domain", "server_agent", "bench", "environment", "status"],
                as_dict=True,
            )
            if (
                not site or site.server_agent != server_agent or site.bench != bench
                or site.environment != agent.environment
            ):
                raise OperationAuthoringError("site ownership is inconsistent")
            site_name = site.name
            site_domain = site.domain
            site_active = site.status in {"active", "maintenance"}

        return TargetSnapshot(
            server_agent=agent.name,
            agent_id=agent.agent_id,
            bench=bench_row.name,
            bench_id=bench_row.bench_id,
            managed_site=site_name,
            site_domain=site_domain,
            environment=agent.environment,
            inventory_revision=agent.inventory_digest or "",
            capabilities=agent_caps & bench_caps,
            agent_enabled=bool(agent.enabled) and agent.status != "Disabled" and bool(bench_row.enabled),
            site_active=site_active,
        )

    def approval_rules(self) -> tuple[ApprovalRule, ...]:
        rows = self.frappe.get_all(
            "Approval Policy",
            filters={"enabled": 1},
            fields=["name", "environment", "operation_pattern", "minimum_approvals", "require_backup", "enabled"],
            order_by="name asc",
            limit_page_length=1000,
        )
        return tuple(
            ApprovalRule(
                name=row.name,
                environment=row.environment,
                operation_pattern=row.operation_pattern,
                minimum_approvals=int(row.minimum_approvals),
                require_backup=bool(row.require_backup),
                enabled=bool(row.enabled),
            )
            for row in rows
        )

    def _existing_is_identical(self, operation: AuthoredOperation) -> bool:
        row = self.db.get_value(
            "Operation", operation.operation_id,
            [
                "operation_id", "idempotency_key", "protocol_version", "server_agent",
                "bench", "managed_site", "operation_type", "payload_json", "payload_hash",
                "requested_by", "approval_policy", "required_approvals",
                "preview_of", "preview_result_hash",
            ],
            as_dict=True,
        )
        if not row:
            return False
        expected = asdict(operation)
        for field in (
            "operation_id", "idempotency_key", "protocol_version", "server_agent", "bench",
            "managed_site", "operation_type", "payload_json", "payload_hash", "requested_by",
            "approval_policy", "required_approvals",
            "preview_of", "preview_result_hash",
        ):
            if row.get(field) != expected[field]:
                raise OperationAuthoringError("operation id is already bound to different data")
        target = self.db.get_value(
            "Operation Target", {"operation": operation.operation_id},
            [
                "target_key", "server_agent", "bench", "managed_site", "site_domain_snapshot",
                "operation_type_snapshot", "payload_hash_snapshot", "inventory_revision_snapshot",
            ],
            as_dict=True,
        )
        expected_target = (
            operation.target_key, operation.server_agent, operation.bench,
            operation.managed_site, operation.site_domain, operation.operation_type,
            operation.payload_hash, operation.inventory_revision,
        )
        if not target or tuple(target.get(field) for field in (
            "target_key", "server_agent", "bench", "managed_site", "site_domain_snapshot",
            "operation_type_snapshot", "payload_hash_snapshot", "inventory_revision_snapshot",
        )) != expected_target:
            raise OperationAuthoringError("operation target is already bound to different data")
        return True

    def create(self, operation: AuthoredOperation) -> Any:
        if self.db.exists("Operation", operation.operation_id):
            if self._existing_is_identical(operation):
                return self.frappe.get_doc("Operation", operation.operation_id)
            raise OperationAuthoringError("operation id already exists")
        operation_doc = self.frappe.get_doc({
            "doctype": "Operation",
            "operation_id": operation.operation_id,
            "idempotency_key": operation.idempotency_key,
            "protocol_version": operation.protocol_version,
            "server_agent": operation.server_agent,
            "bench": operation.bench,
            "managed_site": operation.managed_site,
            "operation_type": operation.operation_type,
            "payload_json": operation.payload_json,
            "payload_hash": operation.payload_hash,
            "requested_by": operation.requested_by,
            "approval_policy": operation.approval_policy,
            "approval_status": operation.approval_status,
            "required_approvals": operation.required_approvals,
            "approval_count": 0,
            "state": operation.state,
            "preview_of": operation.preview_of,
            "preview_result_hash": operation.preview_result_hash,
        }).insert(ignore_permissions=True)
        self.frappe.get_doc({
            "doctype": "Operation Target",
            "operation": operation_doc.name,
            "target_key": operation.target_key,
            "server_agent": operation.server_agent,
            "bench": operation.bench,
            "managed_site": operation.managed_site,
            "site_domain_snapshot": operation.site_domain,
            "operation_type_snapshot": operation.operation_type,
            "payload_hash_snapshot": operation.payload_hash,
            "inventory_revision_snapshot": operation.inventory_revision,
            "state": operation.state,
            "attempt": 0,
        }).insert(ignore_permissions=True)
        return operation_doc


__all__ = ["FrappeOperationAuthoringRepository"]
