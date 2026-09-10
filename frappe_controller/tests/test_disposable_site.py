"""Database-backed installation invariants for the disposable Frappe gate."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_controller.frappe_repository import (
    FrappeIngestionRepository,
    FrappeInventoryRepository,
)
from frappe_controller.bulk import (
    BulkExecutionPolicy,
    BulkPlan,
    BulkSelector,
    BulkTargetSnapshot,
)
from frappe_controller.bulk_orchestrator import BulkOrchestrator
from frappe_controller.frappe_bulk_orchestration import FrappeBulkRepository
from frappe_controller.frappe_store import FrappeCommandStore
from frappe_controller.dispatcher import DispatchConflict, immutable_command_hash
from frappe_controller.api.operations import create_operation, start_operation
from frappe_controller.api.data_updates import preview_data_update, promote_data_update
from frappe_controller.data_update_authoring import canonical_hash
from frappe_controller.ingestion import ControllerIngestionService
from frappe_controller.inventory import InventoryReconciliationService


class TestDisposableControllerSite(FrappeTestCase):
    @staticmethod
    def _insert_agent(agent_id: str):
        return frappe.get_doc(
            {
                "doctype": "Server Agent",
                "agent_id": agent_id,
                "server_name": "Disposable integration agent",
                "environment": "development",
                "enabled": 1,
                "status": "Enrolling",
                "protocol_version": "1.0",
                "audience": "frappe-controller",
            }
        ).insert(ignore_permissions=True)

    @staticmethod
    def _insert_bench(agent_id: str, bench_id: str):
        return frappe.get_doc(
            {
                "doctype": "Bench",
                "bench_id": bench_id,
                "server_agent": agent_id,
                "display_name": bench_id,
                "environment": "development",
                "enabled": 1,
                "installed_apps_json": "[]",
                "capabilities_json": "[]",
            }
        ).insert(ignore_permissions=True)

    @staticmethod
    def _insert_site(agent_id: str, bench_id: str, site_id: str, domain: str):
        return frappe.get_doc(
            {
                "doctype": "Managed Site",
                "site_id": site_id,
                "domain": domain,
                "server_agent": agent_id,
                "bench": bench_id,
                "environment": "development",
                "status": "active",
                "installed_apps_json": "[]",
                "scheduler_enabled": 1,
                "maintenance_mode": 0,
                "health_status": "healthy",
            }
        ).insert(ignore_permissions=True)

    @staticmethod
    def _insert_user(email: str, role: str):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": email.split("@", 1)[0],
                "enabled": 1,
                "send_welcome_email": 0,
            }
        ).insert(ignore_permissions=True)
        user.add_roles(role)
        return user

    @staticmethod
    def _bulk_parent(policy_name: str, requested_by: str):
        operation_id = str(uuid.uuid4())
        payload_json = "{}"
        selector_json = json.dumps(
            {"exact_site_ids": ["bulk-site-1"]},
            sort_keys=True,
            separators=(",", ":"),
        )
        document = frappe.get_doc(
            {
                "doctype": "Bulk Operation",
                "bulk_operation_id": operation_id,
                "idempotency_key": f"controller-bulk:{operation_id}",
                "operation_type": "site.backup",
                "payload_json": payload_json,
                "payload_hash": hashlib.sha256(payload_json.encode()).hexdigest(),
                "selector_json": selector_json,
                "selector_hash": hashlib.sha256(selector_json.encode()).hexdigest(),
                "snapshot_hash": "a" * 64,
                "dry_run_hash": "b" * 64,
                "environment_counts_json": '{"development":1}',
                "requested_by": requested_by,
                "approval_policy": policy_name,
                "approval_status": "pending",
                "required_approvals": 1,
                "approval_count": 0,
                "maximum_targets": 1,
                "canary_size": 1,
                "global_concurrency": 1,
                "per_agent_concurrency": 1,
                "per_bench_concurrency": 1,
                "maximum_failures": 0,
                "maximum_failure_ratio": 0.0,
                "state": "awaiting_approval",
                "total_targets": 1,
                "planned_count": 1,
                "queued_count": 0,
                "running_count": 0,
                "succeeded_count": 0,
                "failed_count": 0,
                "cancelled_count": 0,
                "needs_intervention_count": 0,
                "pause_requested": 0,
                "cancel_requested": 0,
                "requested_at": datetime.now(UTC),
                "previewed_at": datetime.now(UTC),
            }
        )
        return document

    @staticmethod
    def _insert_operation(
        agent_id: str,
        bench_id: str,
        *,
        state: str,
        approval_policy: str | None = None,
        required_approvals: int = 0,
    ):
        operation_id = str(uuid.uuid4())
        payload_json = "{}"
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        operation = frappe.get_doc(
            {
                "doctype": "Operation",
                "operation_id": operation_id,
                "idempotency_key": f"controller:{operation_id}",
                "protocol_version": "1.0",
                "server_agent": agent_id,
                "bench": bench_id,
                "operation_type": "site.backup",
                "payload_json": payload_json,
                "payload_hash": payload_hash,
                "requested_by": "Administrator",
                "approval_policy": approval_policy,
                "approval_status": "pending" if required_approvals else "not_required",
                "required_approvals": required_approvals,
                "approval_count": 0,
                "state": state,
            }
        ).insert(ignore_permissions=True)
        frappe.get_doc(
            {
                "doctype": "Operation Target",
                "operation": operation.name,
                "target_key": f"{operation_id}:{bench_id}",
                "server_agent": agent_id,
                "bench": bench_id,
                "operation_type_snapshot": "site.backup",
                "payload_hash_snapshot": payload_hash,
                "state": state,
            }
        ).insert(ignore_permissions=True)
        return operation

    def test_app_schema_and_roles_are_really_installed(self) -> None:
        self.assertIn("frappe_controller", frappe.get_installed_apps())
        for doctype in (
            "Server Agent",
            "Agent Certificate",
            "Bench",
            "Managed Site",
            "Operation",
            "Operation Target",
            "Operation Event",
            "Approval Policy",
            "Operation Approval",
            "Bulk Operation",
            "Bulk Operation Target",
            "Bulk Operation Approval",
        ):
            self.assertTrue(frappe.db.exists("DocType", doctype), doctype)
            table = f"tab{doctype}"
            self.assertTrue(frappe.db.table_exists(table), table)
        operation_columns = set(frappe.db.get_table_columns("Operation"))
        target_columns = set(frappe.db.get_table_columns("Operation Target"))
        self.assertTrue({"bulk_parent", "bulk_target", "retry_of"} <= operation_columns)
        self.assertTrue({"bulk_parent", "bulk_target", "retry_of"} <= target_columns)
        for role in ("Controller Admin", "Operator", "Approver", "Auditor"):
            self.assertTrue(frappe.db.exists("Role", role), role)

    def test_default_off_feature_file_blocks_new_erp_operation(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        agent_id = f"flags-agent-{suffix}"
        bench_id = f"flags-bench-{suffix}"
        domain = f"flags-{suffix}.example.invalid"
        operator = self._insert_user(
            f"flags-operator-{suffix}@example.invalid", "Operator"
        )
        self._insert_agent(agent_id)
        self._insert_bench(agent_id, bench_id)
        self._insert_site(agent_id, bench_id, domain, domain)
        path = Path(frappe.get_site_path(f"disabled-features-{suffix}.json"))
        path.write_text(json.dumps({
            "schema_version": 1,
            "master_enabled": False,
            "features": {
                "inventory": [],
                "backup_and_verify": [],
                "routine_operations": [],
                "restore_and_reinstall": [],
                "quarantine_delete": [],
                "data_update_preview": [],
                "data_update_apply": [],
                "bulk_operations": [],
                "agent_upgrades": [],
            },
        }), encoding="utf-8")
        path.chmod(0o640)
        key = "frappe_controller_feature_flags_path"
        previous = frappe.conf.get(key)
        request_json = json.dumps({
            "operation_id": str(uuid.uuid4()),
            "operation_type": "site.backup",
            "server_agent": agent_id,
            "bench": bench_id,
            "managed_site": domain,
            "payload": {"domain": domain},
        }, sort_keys=True, separators=(",", ":"))
        try:
            frappe.conf[key] = str(path)
            frappe.set_user(operator.name)
            before = frappe.db.count("Operation")
            with self.assertRaises(frappe.PermissionError):
                create_operation(request_json)
            self.assertEqual(before, frappe.db.count("Operation"))
        finally:
            frappe.set_user("Administrator")
            if previous is None:
                frappe.conf.pop(key, None)
            else:
                frappe.conf[key] = previous
            path.unlink(missing_ok=True)

    def test_server_agent_controller_and_database_constraints_execute(self) -> None:
        agent_id = "disposable-frappe-agent"
        document = self._insert_agent(agent_id)
        self.assertEqual(agent_id, document.name)
        self.assertEqual(agent_id, frappe.db.get_value("Server Agent", agent_id, "agent_id"))

        duplicate = frappe.copy_doc(document)
        duplicate.name = None
        with self.assertRaises(frappe.DuplicateEntryError):
            duplicate.insert(ignore_permissions=True)

        document.protocol_version = "2.0"
        with self.assertRaises(frappe.ValidationError):
            document.save(ignore_permissions=True)

    def test_authenticated_lifecycle_authoring_resolves_policy_and_queues_after_approval(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        agent_id = f"author-agent-{suffix}"
        bench_id = f"author-bench-{suffix}"
        domain = f"author-{suffix}.example.invalid"
        operator = self._insert_user(f"author-operator-{suffix}@example.invalid", "Operator")
        approver = self._insert_user(f"author-approver-{suffix}@example.invalid", "Approver")
        agent = self._insert_agent(agent_id)
        agent.status = "Online"
        agent.inventory_digest = "d" * 64
        agent.capabilities_json = '["site.backup"]'
        agent.save(ignore_permissions=True)
        bench = self._insert_bench(agent_id, bench_id)
        bench.capabilities_json = '["site.backup"]'
        bench.save(ignore_permissions=True)
        site = self._insert_site(agent_id, bench_id, domain, domain)
        policy = frappe.get_doc({
            "doctype": "Approval Policy",
            "policy_name": f"Author policy {suffix}",
            "enabled": 1,
            "environment": "development",
            "operation_pattern": "site.backup",
            "minimum_approvals": 1,
            "require_distinct_approvers": 1,
            "prohibit_requester_approval": 1,
            "require_backup": 0,
            "bulk_threshold": 1,
            "maximum_targets": 10,
        }).insert(ignore_permissions=True)
        operation_id = str(uuid.uuid4())
        request_json = json.dumps({
            "operation_id": operation_id,
            "operation_type": "site.backup",
            "server_agent": agent.name,
            "bench": bench.name,
            "managed_site": site.name,
            "payload": {"domain": domain},
        }, sort_keys=True, separators=(",", ":"))
        try:
            frappe.set_user(operator.name)
            authored = create_operation(request_json)
            self.assertEqual("awaiting_approval", authored["state"])
            stored = frappe.get_doc("Operation", operation_id)
            self.assertEqual(policy.name, stored.approval_policy)
            self.assertEqual(operator.name, stored.requested_by)
            self.assertTrue(frappe.db.exists("Operation Target", {"operation": operation_id}))

            frappe.set_user(approver.name)
            frappe.get_doc({
                "doctype": "Operation Approval",
                "operation": operation_id,
                "approval_policy": policy.name,
                "decision": "approved",
                "comment": "Disposable authoring approval",
            }).insert()

            frappe.set_user(operator.name)
            queued = start_operation(operation_id)
            self.assertEqual("queued", queued["state"])
            self.assertEqual("queued", frappe.db.get_value("Operation", operation_id, "state"))
        finally:
            frappe.set_user("Administrator")

    def test_data_update_preview_result_is_bound_to_promoted_approval_operation(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        agent_id = f"update-agent-{suffix}"
        bench_id = f"update-bench-{suffix}"
        domain = f"update-{suffix}.example.invalid"
        operator = self._insert_user(f"update-operator-{suffix}@example.invalid", "Operator")
        agent = self._insert_agent(agent_id)
        agent.status = "Online"
        agent.inventory_digest = "e" * 64
        agent.capabilities_json = '["data.update"]'
        agent.save(ignore_permissions=True)
        bench = self._insert_bench(agent_id, bench_id)
        bench.capabilities_json = '["data.update"]'
        bench.save(ignore_permissions=True)
        site = self._insert_site(agent_id, bench_id, domain, domain)
        frappe.get_doc({
            "doctype": "Approval Policy",
            "policy_name": f"Data update policy {suffix}",
            "enabled": 1,
            "environment": "development",
            "operation_pattern": "data.update",
            "minimum_approvals": 1,
            "require_distinct_approvers": 1,
            "prohibit_requester_approval": 1,
            "require_backup": 0,
            "bulk_threshold": 1,
            "maximum_targets": 10,
        }).insert(ignore_permissions=True)
        preview_id = str(uuid.uuid4())
        actual_id = str(uuid.uuid4())
        mutation = {
            "contract_version": "1.0",
            "doctype": "Customer",
            "document_names": ["CUST-0001"],
            "filters": None,
            "changes": {"disabled": True},
            "expected_modified": None,
            "dry_run": True,
            "maximum_rows": 1,
            "reason": "Disposable preview verification",
        }
        request_json = json.dumps({
            "operation_id": preview_id,
            "operation_type": "data.update",
            "server_agent": agent.name,
            "bench": bench.name,
            "managed_site": site.name,
            "policy_id": "standard.customer.v1",
            "payload": mutation,
        }, sort_keys=True, separators=(",", ":"))
        try:
            frappe.set_user(operator.name)
            preview_data_update(request_json)
            inner_result = {
                "contract_version": "1.0",
                "operation_id": preview_id,
                "operation": "data.update",
                "policy_id": "standard.customer.v1",
                "policy_version": "1.0",
                "payload_hash": canonical_hash(mutation),
                "actor": operator.name,
                "reason": mutation["reason"],
                "target": {
                    "agent_id": agent_id,
                    "bench_id": bench_id,
                    "site_id": domain,
                    "site_domain": domain,
                    "doctype": "Customer",
                },
                "dry_run": True,
                "maximum_rows": 1,
                "matched_count": 1,
                "affected_count": 1,
                "result": "would_update",
                "evidence": [],
                "evidence_truncated": True,
            }
            outer_result = {
                "status": "succeeded", "operation_id": preview_id,
                "bench_id": bench_id, "site_domain": domain,
                "operation": "data.update", "attempt": 1, "max_attempts": 3,
                "error_code": None, "result": inner_result,
            }
            encoded_result = json.dumps(
                outer_result, sort_keys=True, separators=(",", ":")
            )
            result_hash = hashlib.sha256(encoded_result.encode()).hexdigest()
            frappe.db.set_value("Operation", preview_id, {
                "state": "succeeded", "result_json": encoded_result,
                "result_hash": result_hash,
            }, update_modified=False)
            promoted = promote_data_update(preview_id, actual_id)
            self.assertEqual("awaiting_approval", promoted["state"])
            actual = frappe.get_doc("Operation", actual_id)
            self.assertEqual(preview_id, actual.preview_of)
            self.assertEqual(result_hash, actual.preview_result_hash)
            self.assertFalse(json.loads(actual.payload_json)["payload"]["dry_run"])
        finally:
            frappe.set_user("Administrator")

    def test_transactional_inventory_repository_updates_real_tables(self) -> None:
        agent_id = "repository-integration-agent"
        self._insert_agent(agent_id)
        inventory = {
            "schema_version": 1,
            "inventory_version": "1.0",
            "benches": [
                {
                    "bench_id": "integration-bench",
                    "versions": [["frappe", "15.0.0"]],
                    "capabilities": ["site.backup"],
                    "sites": [],
                }
            ],
        }
        encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
        state = {
            "status": "ready",
            "version": "integration-test",
            "inventory_digest": hashlib.sha256(encoded.encode()).hexdigest(),
            "inventory": inventory,
        }
        service = InventoryReconciliationService(
            FrappeInventoryRepository(), offline_after_seconds=60
        )

        delta = service.reconcile(agent_id, state, observed_at=datetime.now(UTC))

        self.assertEqual(("integration-bench",), delta.added_benches)
        self.assertEqual(agent_id, FrappeInventoryRepository().bench_owner("integration-bench"))
        self.assertEqual(
            1, frappe.db.get_value("Server Agent", agent_id, "inventory_revision")
        )
        self.assertEqual(
            "healthy", frappe.db.get_value("Bench", "integration-bench", "health_status")
        )

    def test_transactional_ingestion_commits_events_and_results(self) -> None:
        agent_id = "ingestion-integration-agent"
        bench_id = "ingestion-bench"
        self._insert_agent(agent_id)
        self._insert_bench(agent_id, bench_id)
        operation = self._insert_operation(agent_id, bench_id, state="queued")
        now = datetime.now(UTC)
        service = ControllerIngestionService(FrappeIngestionRepository())
        common = {
            "protocol_version": "1.0",
            "agent_id": agent_id,
            "audience": "frappe-controller",
            "operation_id": operation.operation_id,
        }

        through = service.ingest_events(
            {
                **common,
                "events": [
                    {
                        "sequence": 1,
                        "attempt": 0,
                        "step": "integration",
                        "kind": "integration.persisted",
                        "details": {},
                        "created_at": now.isoformat(),
                    }
                ],
            },
            received_at=now,
        )
        self.assertEqual(1, through)
        self.assertEqual(
            1, frappe.db.get_value("Operation", operation.name, "last_event_sequence")
        )
        self.assertEqual(
            1,
            frappe.db.count(
                "Operation Event",
                {"operation_id": operation.operation_id, "sequence": 1},
            ),
        )

        service.ingest_result(
            {
                **common,
                "result": {
                    "status": "queued",
                    "operation_id": operation.operation_id,
                    "bench_id": bench_id,
                    "site_domain": None,
                    "operation": "site.backup",
                    "attempt": 0,
                    "max_attempts": 3,
                    "error_code": None,
                    "accepted": True,
                },
            },
            received_at=now,
        )
        stored = frappe.db.get_value(
            "Operation", operation.name, ["state", "result_hash"], as_dict=True
        )
        self.assertEqual("queued", stored.state)
        self.assertRegex(stored.result_hash, r"^[0-9a-f]{64}$")

    def test_approved_dispatch_is_atomically_leased_and_redelivered(self) -> None:
        agent_id = "dispatch-integration-agent"
        bench_id = "dispatch-bench"
        policy_name = "Disposable approval policy"
        self._insert_agent(agent_id)
        self._insert_bench(agent_id, bench_id)
        frappe.get_doc(
            {
                "doctype": "Approval Policy",
                "policy_name": policy_name,
                "enabled": 1,
                "environment": "development",
                "operation_pattern": "site.backup",
                "minimum_approvals": 1,
                "require_distinct_approvers": 1,
                "prohibit_requester_approval": 1,
                "require_backup": 0,
                "bulk_threshold": 1,
                "maximum_targets": 1,
            }
        ).insert(ignore_permissions=True)
        operation = self._insert_operation(
            agent_id,
            bench_id,
            state="awaiting_approval",
            approval_policy=policy_name,
            required_approvals=1,
        )
        approver = frappe.get_doc(
            {
                "doctype": "User",
                "email": "disposable-approver@example.invalid",
                "first_name": "Disposable Approver",
                "enabled": 1,
                "send_welcome_email": 0,
            }
        ).insert(ignore_permissions=True)
        approver.add_roles("Approver")
        try:
            frappe.set_user(approver.name)
            frappe.get_doc(
                {
                    "doctype": "Operation Approval",
                    "operation": operation.name,
                    "approval_policy": policy_name,
                    "decision": "approved",
                    "comment": "disposable integration approval",
                }
            ).insert(ignore_permissions=True)
        finally:
            frappe.set_user("Administrator")
        initial_inventory = "a" * 64
        frappe.db.set_value(
            "Server Agent", agent_id, "inventory_digest", initial_inventory
        )
        frappe.db.set_value(
            "Operation Target", {"operation": operation.name},
            "inventory_revision_snapshot", initial_inventory,
        )

        now = datetime.now(UTC)
        store = FrappeCommandStore(command_lifetime_seconds=60, clock_skew_seconds=5)
        queued = store.enqueue_approved_operation(operation.name, now=now)
        self.assertEqual(agent_id, queued["agent_id"])
        command_json, command_hash = frappe.db.get_value(
            "Operation", operation.name, ["command_json", "command_hash"]
        )
        dispatcher = store.dispatcher
        with self.assertRaisesRegex(DispatchConflict, "persisted command is invalid"):
            dispatcher.validate_persisted(operation.name, "{", command_hash, now=now)
        with self.assertRaisesRegex(DispatchConflict, "persisted command must be an object"):
            dispatcher.validate_persisted(operation.name, "[]", command_hash, now=now)
        with self.assertRaisesRegex(DispatchConflict, "persisted command hash changed"):
            dispatcher.validate_persisted(
                operation.name, command_json, "0" * 64, now=now
            )

        persisted = json.loads(command_json)
        changed_operation = dict(persisted, operation="site.restore")
        with self.assertRaisesRegex(
            DispatchConflict, "persisted command no longer matches operation"
        ):
            dispatcher.validate_persisted(
                operation.name,
                json.dumps(changed_operation),
                immutable_command_hash(changed_operation),
                now=now,
            )
        changed_site = dict(persisted, site_id="another-site")
        with self.assertRaisesRegex(
            DispatchConflict, "persisted command no longer matches operation"
        ):
            dispatcher.validate_persisted(
                operation.name,
                json.dumps(changed_site),
                immutable_command_hash(changed_site),
                now=now,
            )
        invalid_claims = dict(persisted, approval_claims={})
        with self.assertRaisesRegex(
            DispatchConflict, "persisted approval claims are invalid"
        ):
            dispatcher.validate_persisted(
                operation.name,
                json.dumps(invalid_claims),
                immutable_command_hash(invalid_claims),
                now=now,
            )
        changed_claims = dict(persisted)
        changed_claims["approval_claims"] = [
            dict(persisted["approval_claims"][0], approved_by="Administrator")
        ]
        with self.assertRaisesRegex(
            DispatchConflict, "persisted approval claims changed"
        ):
            dispatcher.validate_persisted(
                operation.name,
                json.dumps(changed_claims),
                immutable_command_hash(changed_claims),
                now=now,
            )

        frappe.db.set_value(
            "Operation Target", {"operation": operation.name},
            "payload_hash_snapshot", "c" * 64,
        )
        with self.assertRaisesRegex(DispatchConflict, "operation target snapshot changed"):
            dispatcher.validate_persisted(
                operation.name, command_json, command_hash, now=now
            )
        frappe.db.set_value(
            "Operation Target", {"operation": operation.name},
            "payload_hash_snapshot", operation.payload_hash,
        )
        frappe.db.set_value(
            "Operation Target", {"operation": operation.name},
            "inventory_revision_snapshot", "invalid",
        )
        with self.assertRaisesRegex(
            DispatchConflict, "operation inventory snapshot is invalid"
        ):
            dispatcher.validate_persisted(
                operation.name, command_json, command_hash, now=now
            )
        frappe.db.set_value(
            "Operation Target", {"operation": operation.name},
            "inventory_revision_snapshot", initial_inventory,
        )

        # A later heartbeat is normal and must not invalidate an approved command.
        frappe.db.set_value(
            "Server Agent", agent_id, "inventory_digest", "b" * 64
        )
        first = store.lease_command(agent_id, now=now)
        self.assertEqual(operation.operation_id, first["operation_id"])
        self.assertIsNone(store.lease_command(agent_id, now=now))

        redelivered = store.lease_command(agent_id, now=now + timedelta(seconds=66))
        self.assertEqual(operation.operation_id, redelivered["operation_id"])
        for field in (
            "operation_id",
            "idempotency_key",
            "agent_id",
            "bench_id",
            "operation",
            "payload_hash",
            "approval_claims",
        ):
            self.assertEqual(first[field], redelivered[field], field)

    def test_invalid_queued_command_is_quarantined_without_blocking_next_command(self) -> None:
        agent_id = "quarantine-dispatch-agent"
        bench_id = "quarantine-dispatch-bench"
        self._insert_agent(agent_id)
        self._insert_bench(agent_id, bench_id)
        poisoned = self._insert_operation(agent_id, bench_id, state="awaiting_approval")
        healthy = self._insert_operation(agent_id, bench_id, state="awaiting_approval")

        now = datetime.now(UTC)
        store = FrappeCommandStore()
        store.enqueue_approved_operation(poisoned.name, now=now)
        store.enqueue_approved_operation(healthy.name, now=now)
        frappe.db.set_value(
            "Operation", poisoned.name, "command_hash", "0" * 64,
            update_modified=False,
        )

        leased = store.lease_command(agent_id, now=now)

        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(healthy.operation_id, leased["operation_id"])
        self.assertEqual(
            {"state": "failed", "error_code": "command_snapshot_conflict"},
            dict(frappe.db.get_value(
                "Operation", poisoned.name, ["state", "error_code"], as_dict=True
            )),
        )
        self.assertEqual(
            {"state": "failed", "error_code": "command_snapshot_conflict"},
            dict(frappe.db.get_value(
                "Operation Target", {"operation": poisoned.name},
                ["state", "error_code"], as_dict=True,
            )),
        )

    def test_bulk_persistence_is_service_only_approval_bound_and_single_child(self) -> None:
        agent_id = "bulk-integration-agent"
        bench_id = "bulk-integration-bench"
        site_id = "bulk-site-1"
        domain = "bulk-site-1.example.invalid"
        policy_name = "Disposable bulk policy"
        self._insert_agent(agent_id)
        self._insert_bench(agent_id, bench_id)
        self._insert_site(agent_id, bench_id, site_id, domain)
        frappe.get_doc(
            {
                "doctype": "Approval Policy",
                "policy_name": policy_name,
                "enabled": 1,
                "environment": "development",
                "operation_pattern": "site.backup",
                "minimum_approvals": 1,
                "require_distinct_approvers": 1,
                "prohibit_requester_approval": 1,
                "require_backup": 0,
                "bulk_threshold": 1,
                "maximum_targets": 1,
            }
        ).insert(ignore_permissions=True)
        requester = self._insert_user(
            "bulk-requester@example.invalid", "Operator"
        )
        requester.add_roles("Approver")
        approver = self._insert_user(
            "bulk-approver@example.invalid", "Approver"
        )

        raw_parent = self._bulk_parent(policy_name, requester.name)
        frappe.set_user(requester.name)
        try:
            with self.assertRaises(frappe.PermissionError):
                raw_parent.insert(ignore_permissions=True)
        finally:
            frappe.set_user("Administrator")

        parent = self._bulk_parent(policy_name, requester.name)
        parent.flags.controller_service = True
        parent.insert(ignore_permissions=True)
        target_key = "|".join(
            f"{len(value.encode('utf-8'))}:{value}"
            for value in (agent_id, bench_id, site_id)
        )
        target_values = {
            "doctype": "Bulk Operation Target",
            "bulk_operation": parent.name,
            "target_key": target_key,
            "ordinal": 0,
            "wave": 0,
            "attempt": 0,
            "server_agent": agent_id,
            "bench": bench_id,
            "managed_site": site_id,
            "agent_id_snapshot": agent_id,
            "bench_id_snapshot": bench_id,
            "site_id_snapshot": site_id,
            "site_domain_snapshot": domain,
            "environment_snapshot": "development",
            "inventory_revision": "c" * 64,
            "state": "planned",
        }
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(target_values).insert(ignore_permissions=True)
        target = frappe.get_doc(target_values)
        target.flags.controller_service = True
        target.insert(ignore_permissions=True)

        frappe.set_user(requester.name)
        try:
            self_approval = frappe.get_doc(
                {
                    "doctype": "Bulk Operation Approval",
                    "bulk_operation": parent.name,
                    "approval_policy": policy_name,
                    "decision": "approved",
                }
            )
            self_approval.flags.controller_service = True
            with self.assertRaises(frappe.PermissionError):
                self_approval.insert(ignore_permissions=True)
        finally:
            frappe.set_user("Administrator")

        frappe.set_user(approver.name)
        try:
            raw_approval = frappe.get_doc(
                {
                    "doctype": "Bulk Operation Approval",
                    "bulk_operation": parent.name,
                    "approval_policy": policy_name,
                    "decision": "approved",
                }
            )
            with self.assertRaises(frappe.PermissionError):
                raw_approval.insert(ignore_permissions=True)
            approval = frappe.get_doc(
                {
                    "doctype": "Bulk Operation Approval",
                    "bulk_operation": parent.name,
                    "approval_policy": policy_name,
                    "decision": "approved",
                    "comment": "bound disposable approval",
                }
            )
            approval.flags.controller_service = True
            approval.insert(ignore_permissions=True)
            duplicate = frappe.get_doc(
                {
                    "doctype": "Bulk Operation Approval",
                    "bulk_operation": parent.name,
                    "approval_policy": policy_name,
                    "decision": "approved",
                }
            )
            duplicate.flags.controller_service = True
            with self.assertRaises(frappe.DuplicateEntryError):
                duplicate.insert(ignore_permissions=True)
        finally:
            frappe.set_user("Administrator")

        stored_parent = frappe.db.get_value(
            "Bulk Operation",
            parent.name,
            ["approval_status", "approval_count", "state"],
            as_dict=True,
        )
        self.assertEqual("approved", stored_parent.approval_status)
        self.assertEqual(1, stored_parent.approval_count)
        self.assertEqual("approved", stored_parent.state)
        self.assertEqual(parent.snapshot_hash, approval.snapshot_hash)
        self.assertEqual(parent.payload_hash, approval.payload_hash)
        self.assertEqual(parent.dry_run_hash, approval.dry_run_hash)
        self.assertRegex(approval.decision_hash, r"^[0-9a-f]{64}$")

        child_id = str(uuid.uuid4())
        payload_json = "{}"
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        child = frappe.get_doc(
            {
                "doctype": "Operation",
                "operation_id": child_id,
                "idempotency_key": f"controller:{child_id}",
                "protocol_version": "1.0",
                "server_agent": agent_id,
                "bench": bench_id,
                "managed_site": site_id,
                "bulk_parent": parent.name,
                "bulk_target": target.name,
                "operation_type": "site.backup",
                "payload_json": payload_json,
                "payload_hash": payload_hash,
                "requested_by": requester.name,
                "approval_status": "not_required",
                "required_approvals": 0,
                "approval_count": 0,
                "state": "awaiting_approval",
            }
        ).insert(ignore_permissions=True)
        operation_target = frappe.get_doc(
            {
                "doctype": "Operation Target",
                "operation": child.name,
                "target_key": f"{child_id}:{bench_id}:{site_id}",
                "server_agent": agent_id,
                "bench": bench_id,
                "managed_site": site_id,
                "bulk_parent": parent.name,
                "bulk_target": target.name,
                "site_domain_snapshot": domain,
                "operation_type_snapshot": "site.backup",
                "payload_hash_snapshot": payload_hash,
                "state": "awaiting_approval",
            }
        ).insert(ignore_permissions=True)
        target.child_operation = child.name
        target.save(ignore_permissions=True)
        self.assertEqual(
            child.name,
            frappe.db.get_value("Bulk Operation Target", target.name, "child_operation"),
        )
        self.assertEqual(parent.name, child.bulk_parent)
        self.assertEqual(target.name, child.bulk_target)
        self.assertEqual(target.name, operation_target.bulk_target)

        duplicate_child_id = str(uuid.uuid4())
        duplicate_child = frappe.copy_doc(child)
        duplicate_child.name = None
        duplicate_child.operation_id = duplicate_child_id
        duplicate_child.idempotency_key = f"controller:{duplicate_child_id}"
        with self.assertRaises(frappe.DuplicateEntryError):
            duplicate_child.insert(ignore_permissions=True)

        parent.reload()
        parent.snapshot_hash = "d" * 64
        with self.assertRaises(frappe.ValidationError):
            parent.save(ignore_permissions=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc(
                "Bulk Operation Target", target.name, ignore_permissions=True
            )

    def test_bulk_fanout_runs_canary_then_bounded_wave_to_terminal(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        requester = self._insert_user(
            f"fanout-requester-{suffix}@example.invalid", "Operator"
        )
        approver = self._insert_user(
            f"fanout-approver-{suffix}@example.invalid", "Approver"
        )
        policy_name = f"Disposable fanout policy {suffix}"
        frappe.get_doc({
            "doctype": "Approval Policy",
            "policy_name": policy_name,
            "enabled": 1,
            "environment": "development",
            "operation_pattern": "site.backup",
            "minimum_approvals": 1,
            "require_distinct_approvers": 1,
            "prohibit_requester_approval": 1,
            "require_backup": 0,
            "bulk_threshold": 2,
            "maximum_targets": 3,
        }).insert(ignore_permissions=True)

        revision = "7" * 64
        inventory = []
        for index in range(3):
            agent_id = f"fanout-agent-{index}-{suffix}"
            bench_id = f"fanout-bench-{index}-{suffix}"
            site_id = f"fanout-site-{index}-{suffix}"
            domain = f"fanout-{index}-{suffix}.example.invalid"
            agent = self._insert_agent(agent_id)
            agent.status = "Online"
            agent.inventory_digest = revision
            agent.capabilities_json = '["site.backup"]'
            agent.save(ignore_permissions=True)
            bench = self._insert_bench(agent_id, bench_id)
            bench.capabilities_json = '["site.backup"]'
            bench.save(ignore_permissions=True)
            site = self._insert_site(agent_id, bench_id, site_id, domain)
            inventory.append((agent, bench, site, domain))

        snapshots = tuple(
            BulkTargetSnapshot(
                ordinal=index,
                agent_id=agent.agent_id,
                bench_id=bench.bench_id,
                site_id=site.site_id,
                site_domain=domain,
                environment="development",
                inventory_revision=revision,
            )
            for index, (agent, bench, site, domain) in enumerate(inventory)
        )
        operation_id = str(uuid.uuid4())
        payload_json = "{}"
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        selector = BulkSelector(
            exact_site_ids=tuple(snapshot.site_id for snapshot in snapshots)
        )
        execution_policy = BulkExecutionPolicy(
            maximum_targets=3,
            canary_size=1,
            global_concurrency=2,
            per_agent_concurrency=1,
            per_bench_concurrency=1,
            maximum_failures=0,
            maximum_failure_ratio=0.0,
        )
        plan = BulkPlan(
            parent_operation_id=operation_id,
            operation_type="site.backup",
            payload_hash=payload_hash,
            selector=selector,
            targets=snapshots,
            dry_run_hash="8" * 64,
            policy=execution_policy,
        )
        selector_json = json.dumps(
            {
                "exact_site_ids": list(selector.exact_site_ids),
                "environment": selector.environment,
                "agent_ids": list(selector.agent_ids),
                "bench_ids": list(selector.bench_ids),
                "labels": list(selector.labels),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        parent = frappe.get_doc({
            "doctype": "Bulk Operation",
            "bulk_operation_id": operation_id,
            "idempotency_key": f"controller-bulk:{operation_id}",
            "operation_type": "site.backup",
            "payload_json": payload_json,
            "payload_hash": payload_hash,
            "selector_json": selector_json,
            "selector_hash": hashlib.sha256(selector_json.encode()).hexdigest(),
            "snapshot_hash": plan.snapshot_hash,
            "dry_run_hash": plan.dry_run_hash,
            "environment_counts_json": '{"development":3}',
            "requested_by": requester.name,
            "approval_policy": policy_name,
            "approval_status": "pending",
            "required_approvals": 1,
            "approval_count": 0,
            "maximum_targets": execution_policy.maximum_targets,
            "canary_size": execution_policy.canary_size,
            "global_concurrency": execution_policy.global_concurrency,
            "per_agent_concurrency": execution_policy.per_agent_concurrency,
            "per_bench_concurrency": execution_policy.per_bench_concurrency,
            "maximum_failures": execution_policy.maximum_failures,
            "maximum_failure_ratio": execution_policy.maximum_failure_ratio,
            "state": "awaiting_approval",
            "total_targets": len(snapshots),
            "planned_count": len(snapshots),
            "queued_count": 0,
            "running_count": 0,
            "succeeded_count": 0,
            "failed_count": 0,
            "cancelled_count": 0,
            "needs_intervention_count": 0,
            "pause_requested": 0,
            "cancel_requested": 0,
            "requested_at": datetime.now(UTC),
            "previewed_at": datetime.now(UTC),
        })
        parent.flags.controller_service = True
        parent.insert(ignore_permissions=True)

        target_names = []
        for snapshot, (agent, bench, site, _domain) in zip(
            snapshots, inventory, strict=True
        ):
            target = frappe.get_doc({
                "doctype": "Bulk Operation Target",
                "bulk_operation": parent.name,
                "target_key": snapshot.target_key,
                "ordinal": snapshot.ordinal,
                "wave": 0 if snapshot.ordinal < execution_policy.canary_size else 1,
                "attempt": 0,
                "server_agent": agent.name,
                "bench": bench.name,
                "managed_site": site.name,
                "agent_id_snapshot": snapshot.agent_id,
                "bench_id_snapshot": snapshot.bench_id,
                "site_id_snapshot": snapshot.site_id,
                "site_domain_snapshot": snapshot.site_domain,
                "environment_snapshot": snapshot.environment,
                "inventory_revision": snapshot.inventory_revision,
                "state": "planned",
            })
            target.flags.controller_service = True
            target.insert(ignore_permissions=True)
            target_names.append(target.name)

        try:
            frappe.set_user(approver.name)
            approval = frappe.get_doc({
                "doctype": "Bulk Operation Approval",
                "bulk_operation": parent.name,
                "approval_policy": policy_name,
                "decision": "approved",
                "comment": "Approve the disposable canary and bounded wave",
            })
            approval.flags.controller_service = True
            approval.insert(ignore_permissions=True)
        finally:
            frappe.set_user("Administrator")

        repository = FrappeBulkRepository(frappe)
        orchestrator = BulkOrchestrator(repository)
        self.assertEqual("canary", orchestrator.reconcile(parent.name))
        self.assertEqual(
            ["queued", "planned", "planned"],
            [frappe.db.get_value("Bulk Operation Target", name, "state") for name in target_names],
        )
        self.assertEqual(
            1, frappe.db.count("Operation", {"bulk_parent": parent.name})
        )

        ingestion = ControllerIngestionService(FrappeIngestionRepository())
        store = FrappeCommandStore()

        def finish(agent_id: str, bench_id: str, domain: str) -> str:
            command = store.lease_command(agent_id, now=datetime.now(UTC))
            self.assertIsNotNone(command)
            assert command is not None
            self.assertEqual(1, len(command["approval_claims"]))
            result = {
                "status": "succeeded",
                "operation_id": command["operation_id"],
                "bench_id": bench_id,
                "site_domain": domain,
                "operation": "site.backup",
                "attempt": 1,
                "max_attempts": 3,
                "error_code": None,
                "result": {"backup": "disposable-evidence"},
            }
            ingestion.ingest_result(
                {
                    "protocol_version": "1.0",
                    "agent_id": agent_id,
                    "audience": "frappe-controller",
                    "operation_id": command["operation_id"],
                    "result": result,
                },
                received_at=datetime.now(UTC),
            )
            return command["operation_id"]

        canary_id = finish(
            snapshots[0].agent_id, snapshots[0].bench_id, snapshots[0].site_domain
        )
        self.assertEqual(
            "succeeded",
            frappe.db.get_value("Bulk Operation Target", target_names[0], "state"),
        )
        self.assertEqual("running", orchestrator.reconcile(parent.name))
        self.assertEqual(
            ["succeeded", "queued", "queued"],
            [frappe.db.get_value("Bulk Operation Target", name, "state") for name in target_names],
        )
        self.assertEqual(
            3, frappe.db.count("Operation", {"bulk_parent": parent.name})
        )

        child_ids = {canary_id}
        for snapshot in snapshots[1:]:
            child_ids.add(finish(
                snapshot.agent_id, snapshot.bench_id, snapshot.site_domain
            ))
        self.assertEqual(3, len(child_ids))
        self.assertEqual("succeeded", orchestrator.reconcile(parent.name))
        self.assertEqual("succeeded", orchestrator.reconcile(parent.name))
        self.assertEqual(
            3, frappe.db.count("Operation", {"bulk_parent": parent.name})
        )
        terminal = frappe.db.get_value(
            "Bulk Operation",
            parent.name,
            ["state", "planned_count", "queued_count", "succeeded_count"],
            as_dict=True,
        )
        self.assertEqual("succeeded", terminal.state)
        self.assertEqual(0, terminal.planned_count)
        self.assertEqual(0, terminal.queued_count)
        self.assertEqual(3, terminal.succeeded_count)
