"""Execute the actual receipt endpoint with isolated auth/database adapters."""

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
frappe = ModuleType("frappe")
frappe.whitelist = lambda **_: lambda function: function
utils = ModuleType("frappe.utils")
utils.now_datetime = lambda: "2026-10-02 00:00:00"
security_store = ModuleType("frappe_controller.frappe_security_store")
security_store.FrappeCertificateStore = Mock()
auth = ModuleType("frappe_controller.agent_request_auth")
auth.trusted_peer_and_route_from_frappe_request = Mock(return_value=("peer", "credentials"))
routes = ModuleType("frappe_controller.api.routes")
routes._request_body = Mock()
spec = importlib.util.spec_from_file_location(
    "frappe_controller.api._credential_receipts_test", ROOT / "frappe_controller/api/credential_routes.py"
)
endpoint = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils,
                            security_store.__name__: security_store, auth.__name__: auth, routes.__name__: routes}):
    spec.loader.exec_module(endpoint)


class CredentialReceiptTests(unittest.TestCase):
    def setUp(self):
        self.operation = SimpleNamespace(
            server_agent="agent-1", operation_type="site.create_blank",
            credential_received_at="received", credential_consumed_at="consumed",
            get_password=Mock(return_value="sensitive-value"), save=Mock(),
            flags=SimpleNamespace(controller_service=False),
        )
        self.fake = SimpleNamespace(db=Mock(), request=Mock(), get_doc=Mock(return_value=self.operation))
        self.fake.db.sql.return_value = [("op-1",)]
        self.patcher = patch.object(endpoint, "frappe", self.fake)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        routes._request_body.return_value = json.dumps({
            "protocol_version": "1.0", "agent_id": "agent-1", "audience": "frappe-controller",
            "operation_id": "op-1", "credential": "sensitive-value",
        }).encode()

    def test_consumed_receipt_acknowledges_owner_without_resurrecting_secret(self):
        response = endpoint.store_credential_route()
        self.assertEqual(200, response.status_code)
        self.assertEqual({"accepted": True}, response.get_json())
        self.operation.save.assert_not_called()
        self.operation.get_password.assert_not_called()
        self.assertNotIn("sensitive-value", response.get_data(as_text=True))

    def test_consumed_receipt_never_bypasses_operation_ownership(self):
        self.operation.server_agent = "other-agent"
        response = endpoint.store_credential_route()
        self.assertEqual(403, response.status_code)
        self.operation.save.assert_not_called()

    def test_consumed_without_receipt_is_not_acknowledged(self):
        self.operation.credential_received_at = None
        response = endpoint.store_credential_route()
        self.assertEqual(409, response.status_code)

    def test_unconsumed_replay_still_checks_exact_credential(self):
        self.operation.credential_consumed_at = None
        self.operation.get_password.return_value = "different-secret"
        response = endpoint.store_credential_route()
        self.assertEqual(409, response.status_code)
        self.operation.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
