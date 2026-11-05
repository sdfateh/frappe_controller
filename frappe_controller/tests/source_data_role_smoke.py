"""Verify catalog-only access using an isolated, rolled-back development user."""

from unittest.mock import patch
from uuid import uuid4

import frappe


def verify():
    if not frappe.conf.developer_mode:
        raise RuntimeError("Role verification requires a development site")
    from frappe.boot import get_app_data
    from frappe.desk.desk_views import DeskViews
    from frappe.desk.doctype.sidebar.sidebar import resolve_sidebar
    from frappe_controller.api import source_data, site_actions, operations, approvals
    from frappe_controller.compat import CONTROLLER_ROLES, SOURCE_DATA_ROLE
    from frappe_controller.feature_flags import extend_bootinfo

    actor = frappe.session.user
    user = "source-role-" + uuid4().hex + "@example.invalid"
    try:
        frappe.set_user("Administrator")
        assert frappe.db.exists("Role", SOURCE_DATA_ROLE)
        frappe.get_doc({
            "doctype": "User", "email": user, "first_name": "Source role verification",
            "enabled": 1, "user_type": "System User", "send_welcome_email": 0,
            "roles": [{"role": SOURCE_DATA_ROLE}],
        }).insert(ignore_permissions=True)
        frappe.set_user(user)
        roles = set(frappe.get_roles())
        assert SOURCE_DATA_ROLE in roles and not roles.intersection(CONTROLLER_ROLES)
        for name in frappe.get_all("DocType", filters={"module": "Frappe Controller", "istable": 0}, pluck="name"):
            assert not frappe.has_permission(name, "read"), name
            assert not frappe.has_permission(name, "write"), name
        pages = DeskViews.get_allowed_pages(cache=False)
        assert "source-data" in pages and "site-actions" not in pages
        reports = DeskViews.get_allowed_reports(cache=False)
        assert not set(reports).intersection(frappe.get_all("Report", filters={"module": "Frappe Controller"}, pluck="name"))
        assert "Frappe Controller" not in DeskViews().allowed_workspaces
        sidebar = resolve_sidebar("Frappe Controller", user)
        assert sidebar is not None
        destinations = [row.get("link_to") for row in sidebar.items if row.get("type") == "Link"]
        assert destinations == ["source-data"], destinations
        boot = {"app_data": get_app_data()}
        extend_bootinfo(boot)
        entry = next(app for app in boot["app_data"] if app["app_name"] == "frappe_controller")
        assert entry["on_apps_screen"] and entry["app_title"] == "Source Data"
        assert entry["app_route"] == "/desk/frappe-controller/source-data"
        assert not boot["frappe_controller_features"]["workspace_visible"]
        with patch.object(source_data, "fetch_records", return_value=[]):
            result = source_data.browse("models", filters={"brand": "role-verification-" + uuid4().hex})
        assert result["can_write"] and not result["can_manage_settings"]
        for call in (site_actions._actor, operations._require_author,
                     lambda: approvals.decide_operation(str(uuid4()), "approved")):
            try:
                call()
            except frappe.PermissionError:
                frappe.clear_last_message()
            else:
                raise AssertionError("Catalog role received Controller operation access")
        return {"catalog_crud_role": True, "sidebar_destinations": destinations,
                "controller_records_settings_reports_denied": True,
                "site_actions_and_operations_denied": True,
                "live_catalog_writes": 0, "test_user_rolled_back": True}
    finally:
        frappe.db.rollback()
        frappe.clear_cache(user=user)
        frappe.set_user(actor)
