"""All blank-site authoring paths enforce preflight without breaking retry replay."""
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
spec = importlib.util.spec_from_file_location("frappe_controller.api._creation_admission_test", ROOT / "frappe_controller/api/operations.py")
operations = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"frappe": fake_frappe}):
    spec.loader.exec_module(operations)


def throw(message, exception=ValueError):
    raise exception(message)


class CreationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.frappe = SimpleNamespace(session=SimpleNamespace(user="operator"), get_roles=lambda user: ["Operator"], db=Mock(),
                                     throw=throw, PermissionError=PermissionError, ValidationError=ValueError, conf={})
        self.frappe.db.exists.return_value = False
        self.original = SimpleNamespace(name="old-op", requested_by="operator", operation_type="site.create_blank", state="failed",
            check_permission=Mock(), reload=Mock(), server_agent="agent", bench="bench", managed_site=None,
            payload_json='{"domain":"new.example.com"}', bulk_parent=None, bulk_target=None)
        self.frappe.get_doc = Mock(return_value=self.original)
        self.frappe.db.sql.return_value = []
        for name, value in [
            ("frappe", self.frappe), ("require_operation", Mock()), ("target_environment", Mock(return_value="development")),
            ("FrappeOperationAuthoringRepository", Mock()), ("OperationAuthoringService", Mock()), ("FrappeCommandStore", Mock()),
            ("check_creation_readiness", Mock(return_value={"ready": False, "checks": [{"passed": False, "label": "Agent", "message": "Offline"}]})),
        ]:
            patcher = patch.object(operations, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_generic_create_is_blocked_before_author_or_enqueue(self):
        request = {"operation_id": "1e3746ba-b03d-4a6b-ae86-c63d29ecf608", "operation_type": "site.create_blank",
                   "server_agent": "agent", "bench": "bench", "managed_site": None, "payload": {"domain": "new.example.com"}}
        with self.assertRaisesRegex(ValueError, "Agent: Offline"):
            operations.create_operation(json.dumps(request))
        operations.OperationAuthoringService.assert_not_called()
        operations.FrappeCommandStore.assert_not_called()

    def test_new_retry_is_blocked_before_new_operation(self):
        with self.assertRaisesRegex(ValueError, "Agent: Offline"):
            operations.retry_operation("old-op")
        operations.OperationAuthoringService.assert_not_called()
        operations.FrappeCommandStore.assert_not_called()

    def test_duplicate_retry_returns_original_successor_without_new_check(self):
        self.frappe.db.sql.side_effect = [[], [], [SimpleNamespace(name="existing-retry", state="queued")]]
        self.assertEqual({"operation_id": "existing-retry", "state": "queued"}, operations.retry_operation("old-op"))
        operations.check_creation_readiness.assert_not_called()
        operations.OperationAuthoringService.assert_not_called()

    def test_approval_start_rechecks_before_enqueue(self):
        self.frappe.db.get_value.return_value = SimpleNamespace(requested_by="operator", approval_status="approved", required_approvals=1,
            state="approved", server_agent="agent", operation_type="site.create_blank", payload_json=self.original.payload_json,
            bulk_parent=None, managed_site=None, bench="bench")
        with self.assertRaisesRegex(ValueError, "Agent: Offline"):
            operations.start_operation("1e3746ba-b03d-4a6b-ae86-c63d29ecf608")
        operations.FrappeCommandStore.assert_not_called()

    def test_other_operation_types_use_their_existing_authorization(self):
        operations._check_blank_creation("site.backup", "agent", "bench", {"domain": "new.example.com"})
        operations.check_creation_readiness.assert_not_called()


if __name__ == "__main__":
    unittest.main()
