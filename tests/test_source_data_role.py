"""Catalog management must not grant Controller settings or operation access."""

import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from frappe_controller import compat, feature_flags
from frappe_controller.api import source_data as api


class SourceDataRoleTests(unittest.TestCase):
    def setUp(self):
        def throw(message, error):
            raise error(message)

        self.frappe = SimpleNamespace(
            session=SimpleNamespace(user="catalog@example.invalid"),
            get_roles=lambda user: [compat.SOURCE_DATA_ROLE],
            has_permission=lambda *args, **kwargs: False,
            throw=throw, PermissionError=PermissionError, ValidationError=ValueError,
            db=MagicMock(), cache=MagicMock(),
        )
        self.frappe.db.get_single_value.return_value = None
        self.frappe.cache.get_value.return_value = None
        for context in (
            patch.object(api, "frappe", self.frappe),
            patch.object(api, "runtime_feature_config", return_value=SimpleNamespace(workspace_visible=True)),
            patch.object(api, "_guarded_adapter_factory", return_value=None),
        ):
            context.start()
            self.addCleanup(context.stop)

    def test_catalog_manager_can_read_and_write_without_settings_permission(self):
        with patch.object(api, "fetch_records", return_value=[{"brand": "Example"}]):
            response = api.browse()
        self.assertTrue(response["can_write"])
        self.assertFalse(response["can_manage_settings"])
        with patch.object(api, "_change", return_value={"state": "verified"}) as change:
            api.add({"brand": "Example", "model": "Model"}, str(uuid4()))
            api.edit("vehicles", 1, {"model": "Updated"}, "a" * 64, str(uuid4()))
            api.remove("vehicles", 1, "a" * 64, str(uuid4()))
        self.assertEqual(["add", "edit", "delete"], [call.args[0] for call in change.call_args_list])
        self.assertTrue(all(call.args[4] == "catalog@example.invalid" for call in change.call_args_list))

    def test_unrelated_roles_and_disabled_rollout_cannot_read_or_write(self):
        with patch.object(api, "fetch_records") as fetch, patch.object(api, "_change") as change:
            for actor, roles in [("Guest", [compat.SOURCE_DATA_ROLE]), ("other@example.invalid", ["Desk User"])]:
                self.frappe.session.user = actor
                self.frappe.get_roles = lambda user: roles
                with self.assertRaises(PermissionError):
                    api.browse()
                with self.assertRaises(PermissionError):
                    api.add({}, str(uuid4()))
            self.frappe.session.user = "catalog@example.invalid"
            self.frappe.get_roles = lambda user: [compat.SOURCE_DATA_ROLE]
            api.runtime_feature_config.return_value.workspace_visible = False
            with self.assertRaises(PermissionError):
                api.browse()
            with self.assertRaises(PermissionError):
                api.remove("vehicles", 1, "a" * 64, str(uuid4()))
            fetch.assert_not_called()
            change.assert_not_called()

    def test_catalog_only_app_lands_on_source_data_without_controller_features(self):
        runtime = SimpleNamespace(workspace_visible=True, public_value=lambda: {"workspace_visible": True})
        boot = {"app_data": [{"app_name": "frappe_controller", "app_title": "Frappe Controller",
                              "app_route": "/desk/frappe-controller", "on_apps_screen": True}],
                "allowed_workspaces": [{"name": "Frappe Controller"}, {"name": "Unrelated"}]}
        with patch.dict(sys.modules, {"frappe": self.frappe}), patch.object(feature_flags, "runtime_feature_config", return_value=runtime):
            self.assertTrue(compat.has_app_permission())
            feature_flags.extend_bootinfo(boot)
            self.assertEqual("Source Data", boot["app_data"][0]["app_title"])
            self.assertEqual("/desk/frappe-controller/source-data", boot["app_data"][0]["app_route"])
            self.assertFalse(boot["frappe_controller_features"]["workspace_visible"])
            self.assertFalse(boot["frappe_controller_features"]["master_enabled"])
            self.assertEqual([{"name": "Unrelated"}], boot["allowed_workspaces"])
            runtime.workspace_visible = False
            self.assertFalse(compat.has_app_permission())
            feature_flags.extend_bootinfo(boot)
            self.assertFalse(boot["app_data"][0]["on_apps_screen"])
            self.assertEqual("", boot["app_data"][0]["app_route"])

    def test_combined_roles_keep_controller_navigation(self):
        self.frappe.get_roles = lambda user: [compat.SOURCE_DATA_ROLE, "Controller Admin"]
        runtime = SimpleNamespace(public_value=lambda: {"workspace_visible": True})
        boot = {"app_data": [{"app_name": "frappe_controller", "app_title": "Frappe Controller",
                              "app_route": "/desk/frappe-controller"}]}
        with patch.dict(sys.modules, {"frappe": self.frappe}), patch.object(feature_flags, "runtime_feature_config", return_value=runtime):
            feature_flags.extend_bootinfo(boot)
        self.assertEqual("Frappe Controller", boot["app_data"][0]["app_title"])
        self.assertTrue(boot["frappe_controller_features"]["workspace_visible"])


if __name__ == "__main__":
    unittest.main()
