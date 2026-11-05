import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("register_inventory_target", ROOT / "scripts/register_inventory_target.py")
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)


class ProductionRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / "deployment/erp-production.json").read_text())
        self.frappe = SimpleNamespace(session=SimpleNamespace(user="Administrator"), db=Mock(), get_doc=Mock())
        self.frappe.db.get_value.return_value = None
        self.frappe.db.exists.return_value = False

    def test_plan_never_writes_and_all_records_are_production(self):
        result = registration.register(self.frappe, self.profile)
        self.assertTrue(all(row["action"] == "would_create" for row in result))
        self.frappe.get_doc.assert_not_called()
        values = [row[2] for row in registration.registration_records(self.profile)]
        self.assertTrue(all(row["environment"] == "production" for row in values))
        self.assertEqual(0, values[0]["enabled"])
        self.assertEqual(0, values[1]["enabled"])
        self.assertEqual("[]", values[0]["allowed_operations_json"])
        self.assertTrue(all("inventory_updated_at" not in row for row in values))

    def test_apply_creates_three_records_without_committing_in_service(self):
        registration.register(self.frappe, self.profile, apply=True)
        self.assertEqual(3, self.frappe.get_doc.return_value.insert.call_count)
        self.frappe.db.commit.assert_not_called()

    def test_existing_matching_registration_is_idempotent(self):
        docs = {(dt, name): values for dt, name, values in registration.registration_records(self.profile)}
        self.frappe.db.exists.return_value = True
        self.frappe.get_doc.side_effect = lambda dt, name: docs[(dt, name)]
        result = registration.register(self.frappe, self.profile, apply=True)
        self.assertTrue(all(row["action"] == "unchanged" for row in result))

    def test_conflicting_last_record_prevents_earlier_inserts(self):
        self.frappe.db.exists.side_effect = lambda dt, name: dt == "Managed Site"
        self.frappe.get_doc.return_value = {"site_id": "different"}
        with self.assertRaisesRegex(ValueError, "conflicts"):
            registration.register(self.frappe, self.profile, apply=True)
        self.frappe.get_doc.assert_called_once_with("Managed Site", "erp.kaleam.net")

    def test_rejects_activation_and_nonproduction_profiles(self):
        for key, value in [("enabled", True), ("allowed_operations", ["site.delete"]), ("environment", "development")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                registration.register(self.frappe, {**self.profile, key: value}, apply=True)
        self.frappe.get_doc.assert_not_called()

    def test_rejects_existing_domain_alias(self):
        self.frappe.db.get_value.return_value = "other-site"
        with self.assertRaisesRegex(ValueError, "different identity"):
            registration.register(self.frappe, self.profile, apply=True)
        self.frappe.get_doc.assert_not_called()

    def test_non_admin_cannot_register(self):
        self.frappe.session.user = "operator@example.com"
        self.frappe.get_roles = Mock(return_value=["Operator"])
        with self.assertRaises(PermissionError):
            registration.register(self.frappe, self.profile, apply=True)


if __name__ == "__main__":
    unittest.main()
