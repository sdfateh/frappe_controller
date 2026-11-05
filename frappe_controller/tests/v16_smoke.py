"""Read-only post-migration smoke check, invoked with bench execute."""


def verify():
    import frappe
    import erpnext
    from werkzeug.wrappers import Request
    from frappe_controller.api.enrollment_routes import installation_status_route
    from frappe_controller.compat import desk_route, has_app_permission

    assert frappe.__version__.split(".", 1)[0] == "16"
    assert erpnext.__version__.split(".", 1)[0] == "16"
    doctypes = ["Server Agent", "Bench", "Managed Site", "Controller Operation", "Operation Event", "Operation Approval"]
    assert all(frappe.db.table_exists(doctype) for doctype in doctypes)
    manufacturing = frappe.get_meta("Operation")
    assert manufacturing.module == "Manufacturing"
    assert manufacturing.autoname == "Prompt"
    assert not manufacturing.has_field("operation_id")
    assert frappe.get_meta("Controller Operation").autoname == "field:operation_id"
    assert frappe.db.exists("Workspace", "Frappe Controller")
    assert frappe.db.exists("Page", "site-actions")
    assert "setup_required_apps_json" in frappe.db.get_table_columns("Server Agent")
    assert all(frappe.db.exists("Role", role) for role in ["Controller Admin", "Operator", "Approver", "Auditor"])
    assert desk_route("frappe-controller") == "/desk/frappe-controller"
    entries = frappe.get_hooks("add_to_apps_screen", app_name="frappe_controller")
    assert len(entries) == 1 and entries[0]["route"] == "/desk/frappe-controller"
    assert type(has_app_permission()) is bool
    # Exercise the v16 query-builder-backed APIs with the Controller's projections.
    for doctype in doctypes:
        frappe.get_all(doctype, fields=["name"], order_by="name asc", limit_page_length=1)
    original_request = getattr(frappe.local, "request", None)
    try:
        frappe.local.request = Request.from_values(method="POST", content_type="application/json",
            data=b'{"agent_id":"unsigned-smoke-probe","bench_id":""}')
        assert installation_status_route().status_code == 401
    finally:
        frappe.local.request = original_request
    return {
        "frappe": frappe.__version__, "erpnext": erpnext.__version__,
        "desk_route": desk_route("frappe-controller"), "unsigned_request_rejected": True,
        "manufacturing_operation_restored": True,
        "record_counts": {doctype: frappe.db.count(doctype) for doctype in doctypes + ["Customer"]},
    }
