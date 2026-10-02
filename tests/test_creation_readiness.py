"""Pre-creation checks are read-only, fail closed, and evidence-based."""

from datetime import UTC, datetime, timedelta
import json
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frappe_controller import creation_readiness as readiness
from frappe_controller.frappe_repository import FrappeIngestionRepository
from frappe_controller.ingestion import ControllerIngestionService, IngestedResult, IngestionConflictError
from frappe_controller.inventory import parse_heartbeat_state, InventorySchemaError


NOW = datetime(2026, 10, 2, tzinfo=UTC)


class CreationReadinessTests(unittest.TestCase):
    def setUp(self):
        self.agent = SimpleNamespace(enabled=1, status="Online", reported_status="ready", last_seen=NOW,
            inventory_updated_at=NOW, drain_requested=0, public_ip="8.8.8.8", inventory_digest="a" * 64,
            allowed_site_suffixes_json='["example.com"]', allowed_operations_json='["site.create_blank"]')
        self.bench = SimpleNamespace(enabled=1, server_agent="agent-a", health_status="healthy", inventory_updated_at=NOW,
            installed_apps_json='[["frappe","15"],["erpnext","15"],["mos_pro","1"]]', required_apps_json='["frappe","erpnext","mos_pro"]')
        self.frappe = SimpleNamespace(db=Mock(), PermissionError=PermissionError)
        self.frappe.db.get_value.side_effect = lambda dt, *a, **k: self.agent if dt == "Server Agent" else self.bench
        self.frappe.db.exists.return_value = False
        repository = Mock()
        repository.resolve_target.return_value = SimpleNamespace(agent_enabled=True, capabilities={"site.create_blank"})
        repository.approval_rules.return_value = ()
        patches = {
            "frappe_controller.feature_flags.target_environment": Mock(return_value="development"),
            "frappe_controller.feature_flags.require_operation": Mock(),
            "frappe_controller.frappe_operation_service.FrappeOperationAuthoringRepository": Mock(return_value=repository),
            "frappe_controller.controller_settings.load_controller_settings": Mock(),
            "frappe_controller.creation_readiness.dns_preflight": Mock(),
            "frappe_controller.creation_readiness.ingress_preflight": Mock(),
        }
        self.mocks = patches
        for target, value in patches.items():
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_check(self):
        return readiness.check_creation_readiness(self.frappe, domain="new.example.com", server_agent="agent-a", bench="bench-a", now=NOW)

    def test_complete_checks_and_required_apps(self):
        value = self.run_check()
        self.assertTrue(value["ready"])
        self.assertEqual(["frappe", "erpnext", "mos_pro"], value["required_apps"])
        self.assertEqual(8, len(value["checks"]))
        self.frappe.db.set_value.assert_not_called()
        self.frappe.db.sql.assert_not_called()

    def test_stale_heartbeat_blocks_without_outbound_requests(self):
        for value in [None, NOW - timedelta(seconds=121), NOW + timedelta(seconds=1), "bad"]:
            with self.subTest(value=value):
                self.agent.last_seen = value
                self.assertFalse(self.run_check()["ready"])
        readiness.dns_preflight.assert_not_called()
        readiness.ingress_preflight.assert_not_called()

    def test_disabled_draining_wrong_owner_and_stale_inventory(self):
        for obj, key, value in [(self.agent, "enabled", 0), (self.agent, "drain_requested", 1),
                                (self.agent, "reported_status", "degraded"), (self.agent, "inventory_digest", ""),
                                (self.bench, "server_agent", "wrong"), (self.bench, "enabled", 0),
                                (self.bench, "inventory_updated_at", NOW - timedelta(seconds=301))]:
            old = getattr(obj, key)
            setattr(obj, key, value)
            self.assertFalse(self.run_check()["ready"], key)
            setattr(obj, key, old)

    def test_missing_malformed_policy_or_apps_fail_closed(self):
        for field, value in [("required_apps_json", "[]"), ("required_apps_json", '["frappe",{}]'),
                             ("required_apps_json", '{"frappe":true}'), ("required_apps_json", '["frappe","frappe"]'),
                             ("installed_apps_json", '[["frappe","15"]]'), ("installed_apps_json", '[["frappe","unknown"]]')]:
            old = getattr(self.bench, field)
            setattr(self.bench, field, value)
            self.assertFalse(self.run_check()["ready"], value)
            setattr(self.bench, field, old)

    def test_boundary_suffix_and_operation_policy(self):
        self.agent.allowed_site_suffixes_json = '["ample.com"]'
        self.assertFalse(self.run_check()["ready"])
        self.agent.allowed_site_suffixes_json = '["example.com"]'
        self.agent.allowed_operations_json = "[]"
        self.assertFalse(self.run_check()["ready"])

    def test_dns_and_ingress_errors_do_not_expose_secrets(self):
        readiness.dns_preflight.side_effect = RuntimeError("super-secret-token")
        readiness.ingress_preflight.side_effect = OSError("internal endpoint")
        result = self.run_check()
        self.assertFalse(result["ready"])
        self.assertNotIn("super-secret", json.dumps(result))
        self.assertNotIn("internal endpoint", json.dumps(result))

    def test_feature_and_production_approval_are_enforced(self):
        self.mocks["frappe_controller.feature_flags.target_environment"].return_value = "production"
        self.assertFalse(self.run_check()["ready"])
        self.mocks["frappe_controller.feature_flags.require_operation"].side_effect = PermissionError()
        self.assertFalse(self.run_check()["ready"])


class ProviderReadinessTests(unittest.TestCase):
    def test_dns_only_uses_get_and_conflicts_block(self):
        settings = SimpleNamespace(cloudflare_api_token="s" * 30, cloudflare_zone_id="a" * 32)
        for records, blocked in [([], False), ([{"type": "A"}], True), ([{"type": "CNAME"}], True)]:
            responses = [Mock(), Mock()]
            responses[0].__enter__ = Mock(return_value=SimpleNamespace(read=lambda count: json.dumps({"success": True, "result": {"name": "example.com", "status": "active"}}).encode()))
            responses[1].__enter__ = Mock(return_value=SimpleNamespace(read=lambda count: json.dumps({"success": True, "result": records}).encode()))
            for response in responses:
                response.__exit__ = Mock(return_value=False)
            opener = Mock()
            opener.open.side_effect = responses
            with patch.object(readiness, "build_opener", return_value=opener):
                if blocked:
                    with self.assertRaises(readiness.ReadinessError):
                        readiness.dns_preflight(settings, "new.example.com")
                else:
                    readiness.dns_preflight(settings, "new.example.com")
            self.assertTrue(all(call.args[0].method == "GET" for call in opener.open.call_args_list))

    def test_ingress_disallows_nonpublic_addresses_without_network(self):
        with patch.object(readiness.socket, "create_connection") as connect:
            for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "host.example", None]:
                with self.subTest(address=address), self.assertRaises(readiness.ReadinessError):
                    readiness.ingress_preflight(address)
            connect.assert_not_called()

    def test_handover_requires_all_evidence_and_credential_receipt(self):
        evidence = {"version": 1, "required_apps": ["frappe", "erpnext", "mos_pro"], "apps_verified": True, "public_https_verified": True}
        encoded = lambda: json.dumps({"result": {"readiness": evidence}})
        self.assertTrue(readiness.creation_handover_ready(encoded(), NOW))
        self.assertFalse(readiness.creation_handover_ready(encoded(), None))
        for key in list(evidence):
            old = evidence.pop(key)
            self.assertFalse(readiness.creation_handover_ready(encoded(), NOW))
            evidence[key] = old
        for malformed in [None, "[]", "null", "{}", "broken"]:
            self.assertFalse(readiness.creation_handover_ready(malformed, NOW))


class IngestionGateTests(unittest.TestCase):
    def test_first_legacy_delivery_closes_lease_without_claiming_readiness(self):
        operation_id = "032a11cd-3f87-4084-a814-4fd2c9c35f7a"
        for operation_type in ["site.create", "site.create_blank", "site.create_from_backup"]:
            for previous_state in ["queued", "leased", "running"]:
                with self.subTest(operation_type=operation_type, state=previous_state):
                    row = {"name": operation_id, "operation_id": operation_id, "agent_id": "agent-a",
                        "bench_id": "bench-a", "site_domain": None, "operation_type": operation_type,
                        "payload_json": '{"domain":"old.example.com"}', "state": previous_state,
                        "result_hash": None if previous_state == "leased" else "a" * 64,
                        "result_json": None if previous_state == "leased" else json.dumps({"status": previous_state}),
                        "required_apps_json": '["frappe","erpnext","mos_pro"]' if previous_state == "running" else "[]",
                        "credential_received_at": NOW if previous_state == "running" else None,
                        "bulk_target": "bulk-target"}
                    frappe = SimpleNamespace(db=Mock())
                    repository = FrappeIngestionRepository(frappe)
                    repository._operation_row = Mock(return_value=row)
                    def write(doctype, name, values, **kwargs):
                        if doctype == "Operation":
                            row.update(values)
                    frappe.db.set_value.side_effect = write
                    service = ControllerIngestionService(repository)
                    request = {"protocol_version": "1.0", "audience": "frappe-controller",
                        "agent_id": "agent-a", "operation_id": operation_id, "result": {
                            "status": "succeeded", "operation_id": operation_id, "bench_id": "bench-a",
                            "site_domain": "old.example.com", "operation": operation_type,
                            "attempt": 1, "max_attempts": 1, "error_code": None,
                            "result": {"domain": "old.example.com"},
                        }}
                    accepted = service.ingest_result(request, received_at=NOW)
                    self.assertEqual("needs_intervention", row["state"])
                    self.assertEqual("site_handover_unverified", row["error_code"])
                    self.assertIsNone(row["lease_expires_at"])
                    self.assertIsNotNone(row["completed_at"])
                    self.assertEqual(accepted.result_json, row["result_json"])
                    self.assertEqual(accepted.body_hash, row["result_hash"])
                    self.assertEqual("succeeded", repository.result(operation_id).status)
                    for call in frappe.db.set_value.call_args_list:
                        self.assertEqual("needs_intervention", call.args[2]["state"])
                        self.assertEqual("site_handover_unverified", call.args[2]["error_code"])
                    self.assertFalse(readiness.creation_handover_ready(row["result_json"], NOW))
                    # Lost ACK: replay must not write, reopen, or promote the job.
                    frappe.db.set_value.reset_mock()
                    self.assertEqual(accepted.body_hash, service.ingest_result(request, received_at=NOW).body_hash)
                    frappe.db.set_value.assert_not_called()
                    request["result"]["result"]["domain"] = "changed.example.com"
                    with self.assertRaisesRegex(IngestionConflictError, "terminal result immutable"):
                        service.ingest_result(request, received_at=NOW)

    def test_malformed_readiness_is_not_treated_as_legacy(self):
        for evidence in [None, {}, [], {"version": 99}]:
            with self.subTest(evidence=evidence):
                frappe = SimpleNamespace(db=Mock())
                repository = FrappeIngestionRepository(frappe)
                repository._operation_row = Mock(return_value={"name": "op", "agent_id": "agent-a",
                    "operation_type": "site.create_blank", "state": "leased"})
                result = IngestedResult("op", "succeeded", "b" * 64,
                    json.dumps({"result": {"readiness": evidence}}), None, NOW)
                with self.assertRaises(IngestionConflictError):
                    repository.commit_result("agent-a", "op", expected_previous_hash=None, result=result)
                frappe.db.set_value.assert_not_called()

    def test_no_success_is_committed_without_complete_matching_handover(self):
        evidence = {"version": 1, "required_apps": ["frappe", "erpnext", "mos_pro"], "apps_verified": True, "public_https_verified": True}
        encoded = json.dumps({"status": "succeeded", "result": {"readiness": evidence}})
        row = {"name": "op", "agent_id": "agent-a", "operation_type": "site.create_blank", "state": "running", "credential_received_at": NOW, "required_apps_json": '["frappe","erpnext","mos_pro"]'}
        for receipt, result_json, required, passes in [
            (NOW, encoded, row["required_apps_json"], True),
            (None, encoded, row["required_apps_json"], False),
            (NOW, "{}", row["required_apps_json"], False),
            (NOW, encoded, '["frappe"]', False),
            (NOW, encoded, "[]", False),
        ]:
            with self.subTest(receipt=receipt, required=required):
                frappe = SimpleNamespace(db=Mock())
                repository = FrappeIngestionRepository(frappe)
                repository._operation_row = Mock(return_value={**row, "credential_received_at": receipt, "required_apps_json": required})
                result = IngestedResult("op", "succeeded", "b" * 64, result_json, None, NOW)
                if passes:
                    repository.commit_result("agent-a", "op", expected_previous_hash=None, result=result)
                    self.assertEqual("succeeded", frappe.db.set_value.call_args_list[0].args[2]["state"])
                else:
                    with self.assertRaises(IngestionConflictError):
                        repository.commit_result("agent-a", "op", expected_previous_hash=None, result=result)
                    frappe.db.set_value.assert_not_called()

    def test_duplicate_legacy_success_is_still_idempotent(self):
        frappe = SimpleNamespace(db=Mock())
        repository = FrappeIngestionRepository(frappe)
        repository._operation_row = Mock(return_value={"agent_id": "agent-a", "operation_type": "site.create_blank", "result_hash": "b" * 64})
        repository.commit_result("agent-a", "op", expected_previous_hash=None, result=IngestedResult("op", "succeeded", "b" * 64, "{}", None, NOW))
        frappe.db.set_value.assert_not_called()

    def test_inventory_preserves_legacy_digest_and_advertises_policy(self):
        for apps in [None, ["frappe", "erpnext", "mos_pro"], [], ["frappe", "frappe"], ["erpnext"]]:
            bench = {"bench_id": "bench-a", "versions": [["frappe", "15"]], "capabilities": ["site.create_blank"], "sites": []}
            if apps is not None:
                bench["required_apps"] = apps
            inventory = {"schema_version": 1, "inventory_version": "1.0", "benches": [bench]}
            canonical = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
            state = {"status": "ready", "version": "1.0", "inventory": inventory, "inventory_digest": hashlib.sha256(canonical.encode()).hexdigest()}
            if apps is None or apps == ["frappe", "erpnext", "mos_pro"]:
                parsed = parse_heartbeat_state(state)
                self.assertEqual(canonical, parsed.inventory.canonical_json())
                self.assertEqual(None if apps is None else tuple(apps), parsed.inventory.benches[0].required_apps)
            else:
                with self.assertRaises(InventorySchemaError):
                    parse_heartbeat_state(state)


if __name__ == "__main__":
    unittest.main()
