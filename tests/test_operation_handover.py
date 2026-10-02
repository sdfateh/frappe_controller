"""Exercise the actual handover endpoint, including its Bench policy lookup."""

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
fake_frappe = ModuleType("frappe")
fake_frappe.whitelist = lambda **kwargs: lambda function: function
spec = importlib.util.spec_from_file_location(
    "frappe_controller.api._operation_handover_test",
    ROOT / "frappe_controller/api/operation_handover.py",
)
handover = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"frappe": fake_frappe}):
    spec.loader.exec_module(handover)


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.apps = ["frappe", "erpnext", "mos_pro"]
        self.operation = SimpleNamespace(
            name="op", bench="bench-a", operation_type="site.create_blank", state="succeeded",
            credential_received_at="2026-10-02", check_permission=Mock(),
            result_json=json.dumps({"result": {"readiness": {
                "version": 1, "required_apps": self.apps,
                "apps_verified": True, "public_https_verified": True,
            }}}),
        )
        self.operation.get = lambda key: getattr(self.operation, key, None)
        self.customer = SimpleNamespace(
            name="Customer A", controller_production_managed_site="site-a", check_permission=Mock(),
        )
        self.frappe = SimpleNamespace(db=Mock(), get_all=Mock(return_value=[self.customer]),
            get_doc=lambda dt, name: self.operation if dt == "Operation" else self.customer)
        self.frappe.db.get_value.return_value = json.dumps(self.apps)
        patcher = patch.object(handover, "frappe", self.frappe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_uses_bench_policy_not_nonexistent_operation_field(self):
        schema = json.loads((ROOT / "frappe_controller/frappe_controller/doctype/operation/operation.json").read_text())
        self.assertNotIn("required_apps_json", [field["fieldname"] for field in schema["fields"]])
        self.assertTrue(handover.get_site_handover_status("op")["ready"])
        self.frappe.db.get_value.assert_called_once_with("Bench", "bench-a", "required_apps_json")
        self.operation.check_permission.assert_called_once_with("read")
        self.customer.check_permission.assert_called_once_with("read")

    def test_missing_or_changed_bench_policy_fails_closed(self):
        for policy in [None, "[]", "broken", '["frappe"]']:
            with self.subTest(policy=policy):
                self.frappe.db.get_value.return_value = policy
                self.assertFalse(handover.get_site_handover_status("op")["ready"])

    def test_customer_link_receipt_and_success_are_required(self):
        for doc, field, value in [
            (self.customer, "controller_production_managed_site", None),
            (self.operation, "credential_received_at", None),
            (self.operation, "state", "needs_intervention"),
            (self.operation, "result_json", '{"result": {"domain": "site-a"}}'),
        ]:
            with self.subTest(field=field):
                original = getattr(doc, field)
                setattr(doc, field, value)
                self.assertFalse(handover.get_site_handover_status("op")["ready"])
                setattr(doc, field, original)

    def test_generic_create_does_not_require_a_customer(self):
        self.frappe.get_all.return_value = []
        result = handover.get_site_handover_status("op")
        self.assertFalse(result["customer_required"])
        self.assertTrue(result["ready"])

    def test_read_permission_is_enforced(self):
        for document in [self.operation, self.customer]:
            document.check_permission.side_effect = PermissionError
            with self.assertRaises(PermissionError):
                handover.get_site_handover_status("op")
            document.check_permission.side_effect = None

    def test_non_creation_operation_is_not_applicable(self):
        self.operation.operation_type = "site.backup"
        self.assertEqual({"applicable": False}, handover.get_site_handover_status("op"))
        self.frappe.db.get_value.assert_not_called()


if __name__ == "__main__":
    unittest.main()
