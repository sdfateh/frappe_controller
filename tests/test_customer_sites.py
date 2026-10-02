"""Customer environment eligibility without a running Frappe database."""

import importlib.util
import json
import sqlite3
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from frappe_controller import feature_flags
from frappe_controller.operation_service import ApprovalRule, OperationAuthoringError, TargetSnapshot


class ValidationError(Exception):
    pass


def throw(message, exception=ValidationError):
    raise exception(message)


fake_frappe = ModuleType("frappe")
fake_frappe.whitelist = lambda **kwargs: lambda function: function
spec = importlib.util.spec_from_file_location(
    "frappe_controller.api._customer_sites_test",
    ROOT / "frappe_controller/api/customer_sites.py",
)
customer_sites = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"frappe": fake_frappe}):
    spec.loader.exec_module(customer_sites)


class CustomerSiteTests(unittest.TestCase):
    def setUp(self):
        self.environment = "development"
        self.operations = {}
        self.sites = {}
        self.customer_values = {}
        self.customer = SimpleNamespace(
            flags=SimpleNamespace(controller_managed_site_update=False),
            get=self.customer_values.get,
            set=lambda field, value: self.customer_values.update({field: value}),
            check_permission=Mock(),
            save=Mock(side_effect=self.validate_links),
        )
        self.frappe = SimpleNamespace(
            session=SimpleNamespace(user="operator@example.com"),
            get_roles=Mock(return_value=["Operator"]),
            db=Mock(), conf={}, as_json=json.dumps, throw=throw,
            ValidationError=ValidationError, DoesNotExistError=ValidationError,
            PermissionError=PermissionError, get_doc=self.get_doc,
        )
        self.frappe.db.sql.return_value = [("Customer A",)]
        self.frappe.db.exists.side_effect = lambda dt, value: dt == "Customer"
        self.frappe.db.get_value.side_effect = lambda dt, key, *args, **kwargs: (
            self.environment if dt == "Server Agent" else self.sites.get(key.get("domain"))
            if dt == "Managed Site" else None
        )
        self.repo = Mock()
        self.repo.create.side_effect = lambda operation: self.operations.update({
            operation.operation_id: SimpleNamespace(
                name=operation.operation_id, state=operation.state,
                operation_type=operation.operation_type, server_agent=operation.server_agent,
                bench=operation.bench, payload_json=operation.payload_json,
                result_json=json.dumps({"result": {"readiness": {"version": 1, "required_apps": ["frappe"], "apps_verified": True, "public_https_verified": True}}}),
                credential_received_at="2026-10-02 00:00:00",
            )
        })
        self.repo.approval_rules.return_value = ()
        self.repo.resolve_target.side_effect = lambda *args: TargetSnapshot(
            server_agent="server-01", agent_id="server-01", bench="bench-a",
            bench_id="bench-a", managed_site=None, site_domain=None,
            environment=self.environment, inventory_revision="a" * 64,
            capabilities=frozenset({"site.create_blank"}),
        )
        flags = {name: frozenset() for name in feature_flags.FEATURES}
        flags.update(inventory=frozenset(feature_flags.ENVIRONMENTS),
                     restore_and_reinstall=frozenset(feature_flags.ENVIRONMENTS))
        self.config = feature_flags.FeatureConfig(True, flags)
        for target, value in [
            ("frappe", self.frappe),
            ("FrappeOperationAuthoringRepository", Mock(return_value=self.repo)),
            ("FrappeCommandStore", Mock()),
            ("check_creation_readiness", Mock(return_value={"ready": True, "checks": [], "required_apps": ["frappe"]})),
        ]:
            patcher = patch.object(customer_sites, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(feature_flags, "runtime_feature_config", side_effect=lambda *args: self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        customer_sites.FrappeCommandStore.return_value.enqueue_approved_operation.side_effect = (
            lambda operation_id, **kwargs: setattr(self.operations[operation_id], "state", "queued")
        )

    def get_doc(self, doctype, name):
        return self.customer if doctype == "Customer" else self.operations[name]

    def validate_links(self, **kwargs):
        # Exercise the real service setter. Fail just as Frappe does for a
        # future Managed Site, instead of mocking that setter out.
        managed_site = self.customer_values.get(customer_sites._MANAGED_SITE_FIELD)
        if managed_site and managed_site not in self.sites:
            raise ValidationError("Could not find Managed Site")
        operation = self.customer_values.get(customer_sites._SITE_OPERATION_FIELD)
        if operation and operation not in self.operations:
            raise ValidationError("Could not find Operation")

    def add_inventory(self, **overrides):
        values = dict(name="customer.example.com", server_agent="server-01", bench="bench-a",
                      status="active", inventory_updated_at="2026-10-02 00:00:00")
        values.update(overrides)
        self.sites["customer.example.com"] = SimpleNamespace(**values)

    def create(self):
        return customer_sites.create_production_site(
            "Customer A", "customer.example.com", "server-01", "bench-a"
        )

    def test_all_supported_environments_can_create(self):
        self.repo.approval_rules.return_value = (
            ApprovalRule(name="production create", environment="production",
                         operation_pattern="site.create_blank", minimum_approvals=1,
                         require_backup=False),
        )
        for environment in feature_flags.ENVIRONMENTS:
            with self.subTest(environment=environment):
                self.environment = environment
                self.customer_values.clear()
                result = self.create()
                self.assertEqual("awaiting_approval" if environment == "production" else "queued", result["state"])
                operation = self.repo.create.call_args.args[0]
                self.assertEqual("site.create_blank", operation.operation_type)
                self.assertEqual("operator@example.com", operation.requested_by)
        self.assertEqual(3, self.repo.create.call_count)
        self.assertEqual(2, customer_sites.FrappeCommandStore.return_value.enqueue_approved_operation.call_count)

    def test_production_still_requires_an_approval_policy(self):
        self.environment = "production"
        with self.assertRaisesRegex(ValidationError, "no approval policy"):
            self.create()
        self.repo.create.assert_not_called()

    def test_disabled_features_still_block_creation(self):
        self.config = feature_flags.FeatureConfig.disabled()
        with self.assertRaises(PermissionError):
            self.create()
        self.repo.create.assert_not_called()
        self.customer.save.assert_not_called()

    def test_unknown_environment_is_rejected(self):
        self.environment = "unknown"
        with self.assertRaises(PermissionError):
            self.create()
        self.repo.create.assert_not_called()

    def test_user_without_author_role_is_rejected(self):
        self.frappe.get_roles.return_value = ["Auditor"]
        with self.assertRaises(PermissionError):
            self.create()
        self.repo.create.assert_not_called()

    def test_environment_approval_rules_are_preserved(self):
        self.repo.approval_rules.return_value = (
            ApprovalRule(name="development approval", environment="development",
                         operation_pattern="site.create_blank", minimum_approvals=1,
                         require_backup=False),
        )
        self.assertEqual("awaiting_approval", self.create()["state"])
        customer_sites.FrappeCommandStore.assert_not_called()

    def test_mismatched_target_is_still_rejected(self):
        self.repo.resolve_target.side_effect = OperationAuthoringError("target environment ownership is inconsistent")
        with self.assertRaisesRegex(ValidationError, "ownership is inconsistent"):
            self.create()
        self.repo.create.assert_not_called()
        self.customer.save.assert_not_called()

    def test_create_saves_existing_operation_not_nonexistent_site(self):
        result = self.create()
        self.assertEqual(result["operation_id"], self.customer_values[customer_sites._SITE_OPERATION_FIELD])
        self.assertFalse(self.customer_values.get(customer_sites._MANAGED_SITE_FIELD))
        self.customer.save.assert_called_once()
        self.assertFalse(self.sites)

    def test_result_before_inventory_waits_then_links_once(self):
        result = self.create()
        self.operations[result["operation_id"]].state = "succeeded"
        self.assertFalse(customer_sites.reconcile_customer_site("Customer A"))
        self.add_inventory()
        self.assertTrue(customer_sites.reconcile_customer_site("Customer A"))
        self.assertEqual("customer.example.com", self.customer_values[customer_sites._MANAGED_SITE_FIELD])
        self.assertFalse(customer_sites.reconcile_customer_site("Customer A"))
        self.assertEqual(2, self.customer.save.call_count)

    def test_inventory_before_result_waits_for_success(self):
        result = self.create()
        self.add_inventory()
        self.assertFalse(customer_sites.reconcile_customer_site("Customer A"))
        self.operations[result["operation_id"]].state = "succeeded"
        self.assertTrue(customer_sites.reconcile_customer_site("Customer A"))

    def test_wrong_owner_missing_inventory_and_failed_operations_do_not_link(self):
        result = self.create()
        operation = self.operations[result["operation_id"]]
        operation.state = "succeeded"
        for mismatch in [{"server_agent": "other"}, {"bench": "other"},
                         {"status": "missing"}, {"inventory_updated_at": None}]:
            self.add_inventory(**mismatch)
            self.assertFalse(customer_sites.reconcile_customer_site("Customer A"))
        self.add_inventory()
        for state in ["failed", "cancelled", "needs_intervention", "timed_out"]:
            operation.state = state
            self.assertFalse(customer_sites.reconcile_customer_site("Customer A"))

    def test_duplicate_click_returns_same_operation(self):
        first = self.create()
        self.assertEqual(first, self.create())
        self.repo.create.assert_called_once()
        self.customer.save.assert_called_once()

    def test_readiness_blocks_authoring_and_linking_without_effects(self):
        customer_sites.check_creation_readiness.return_value = {"ready": False, "checks": [{"passed": False, "label": "Agent", "message": "Offline"}]}
        with self.assertRaisesRegex(ValidationError, "Agent: Offline"):
            self.create()
        self.repo.create.assert_not_called()
        self.customer.save.assert_not_called()

    def test_success_without_handover_evidence_does_not_link(self):
        result = self.create()
        operation = self.operations[result["operation_id"]]
        operation.state = "succeeded"
        self.add_inventory()
        operation.credential_received_at = None
        self.assertFalse(customer_sites.reconcile_customer_site("Customer A"))
        operation.credential_received_at = "2026-10-02 00:00:00"
        operation.result_json = "{}"
        self.assertFalse(customer_sites.reconcile_customer_site("Customer A"))

    def test_different_request_cannot_replace_pending_operation(self):
        self.create()
        with self.assertRaisesRegex(ValidationError, "already has a site creation operation"):
            customer_sites.create_production_site("Customer A", "other.example.com", "server-01", "bench-a")
        self.repo.create.assert_called_once()

    def test_customer_write_permission_is_enforced(self):
        self.customer.check_permission.side_effect = PermissionError("Cannot write Customer")
        with self.assertRaises(PermissionError):
            self.create()
        self.repo.create.assert_not_called()

    def test_users_cannot_change_either_tracking_link(self):
        self.customer.get_doc_before_save = lambda: None
        self.customer.flags = {}
        for field in [customer_sites._MANAGED_SITE_FIELD, customer_sites._SITE_OPERATION_FIELD]:
            self.customer_values.clear()
            self.customer_values[field] = "unauthorized"
            with self.assertRaises(PermissionError):
                customer_sites.protect_managed_site_link(self.customer)


class ReconciliationBatchTests(unittest.TestCase):
    def setUp(self):
        # Execute the production selection query, including its LIMIT/keyset,
        # against an in-memory database. No Controller database is touched.
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)
        self.database.row_factory = sqlite3.Row
        self.database.create_function("JSON_UNQUOTE", 1, lambda value: value)
        self.database.executescript('''
            CREATE TABLE `tabCustomer` (name TEXT, controller_site_creation_operation TEXT,
                controller_production_managed_site TEXT);
            CREATE TABLE `tabOperation` (name TEXT, payload_json TEXT, server_agent TEXT,
                bench TEXT, operation_type TEXT, state TEXT);
            CREATE TABLE `tabManaged Site` (domain TEXT, server_agent TEXT, bench TEXT,
                status TEXT, inventory_updated_at TEXT);
            INSERT INTO `tabOperation` VALUES ('op', '{"domain":"site.example.com"}',
                'agent', 'bench', 'site.create_blank', 'succeeded');
            INSERT INTO `tabManaged Site` VALUES ('site.example.com', 'agent', 'bench', 'active', '2026-10-02');
        ''')
        self.names = [f"Customer {i:03d}" for i in range(101)]
        self.database.executemany("INSERT INTO `tabCustomer` VALUES (?, 'op', NULL)",
                                  [(name,) for name in self.names])
        self.cache_values = {}
        self.cache = Mock()
        self.cache.get_value.side_effect = self.cache_values.get
        self.cache.set_value.side_effect = lambda key, value: self.cache_values.update({key: value})
        self.frappe = SimpleNamespace(cache=Mock(return_value=self.cache), db=Mock())
        def select(query, params, **kwargs):
            return [SimpleNamespace(**dict(row)) for row in self.database.execute(query.replace("%s", "?"), params)]
        self.frappe.db.sql.side_effect = select
        self.reconcile = Mock(return_value=False)
        for key, value in [("frappe", self.frappe), ("reconcile_customer_site", self.reconcile)]:
            patcher = patch.object(customer_sites, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_hundred_ineligible_customers_do_not_starve_later_customer(self):
        linked = []
        def reconcile(name):
            if name == self.names[-1]:
                linked.append(name)
                self.database.execute("UPDATE `tabCustomer` SET controller_production_managed_site='site' WHERE name=?", (name,))
                return True
            return False  # Historical successes with no handover evidence.
        self.reconcile.side_effect = reconcile
        customer_sites.reconcile_customer_sites()
        self.assertEqual(100, self.reconcile.call_count)
        self.assertEqual([], linked)
        self.reconcile.reset_mock()
        customer_sites.reconcile_customer_sites()
        self.reconcile.assert_called_once_with(self.names[-1])
        self.assertEqual([self.names[-1]], linked)
        self.assertEqual("", self.cache_values[customer_sites._RECONCILE_CURSOR_KEY])

    def test_full_batch_wraps_and_revisits_previously_ineligible_customers(self):
        self.database.execute("DELETE FROM `tabCustomer` WHERE name=?", (self.names[-1],))
        customer_sites.reconcile_customer_sites()
        self.assertEqual(self.names[99], self.cache_values[customer_sites._RECONCILE_CURSOR_KEY])
        self.reconcile.reset_mock()
        customer_sites.reconcile_customer_sites()
        self.reconcile.assert_not_called()
        self.assertEqual("", self.cache_values[customer_sites._RECONCILE_CURSOR_KEY])
        customer_sites.reconcile_customer_sites()
        self.assertEqual(100, self.reconcile.call_count)

    def test_cache_loss_repeats_safe_checks(self):
        customer_sites.reconcile_customer_sites()
        self.cache_values.clear()
        self.reconcile.reset_mock()
        customer_sites.reconcile_customer_sites()
        self.assertEqual(100, self.reconcile.call_count)
        self.assertEqual(self.names[0], self.reconcile.call_args_list[0].args[0])

    def test_failed_batch_does_not_advance_cursor(self):
        self.reconcile.side_effect = RuntimeError("transaction failed")
        with self.assertRaises(RuntimeError):
            customer_sites.reconcile_customer_sites()
        self.cache.set_value.assert_not_called()


if __name__ == "__main__":
    unittest.main()
