app_name = "frappe_controller"
app_title = "Frappe Controller"
app_publisher = "Frappe Controller maintainers"
app_description = "Central control plane for allowlisted Frappe server agents"
app_email = "security@example.invalid"
app_license = "MIT"
required_apps = ["frappe", "erpnext"]
app_include_js = ["/assets/frappe_controller/js/controller_workspace.js"]
doctype_js = {
    "Customer": "public/js/customer.js",
}
doc_events = {
    "Customer": {
        "before_save": "frappe_controller.api.customer_sites.protect_managed_site_link",
    },
}
fixtures = [
    {
        "dt": "Custom Field",
        "filters": [["name", "in", ["Customer-controller_production_managed_site"]]],
    },
]
extend_bootinfo = "frappe_controller.feature_flags.extend_bootinfo"

_CONTROLLER_ROLES = ("Controller Admin", "Operator", "Approver", "Auditor")

after_install = "frappe_controller.hooks.ensure_controller_roles"
after_migrate = "frappe_controller.hooks.ensure_controller_roles"
before_install = "frappe_controller.hooks.ensure_controller_roles"

scheduler_events = {
    "cron": {
        "* * * * *": [
            "frappe_controller.frappe_bulk_orchestration.reconcile_bulk_operations"
        ]
    }
}


def ensure_controller_roles():
    """Idempotently install the four least-privilege controller roles."""
    import frappe

    for role_name in _CONTROLLER_ROLES:
        if not frappe.db.exists("Role", role_name):
            frappe.get_doc(
                {
                    "doctype": "Role",
                    "role_name": role_name,
                    "desk_access": 1,
                    "is_custom": 0,
                }
            ).insert(ignore_permissions=True)
    from frappe_controller.operation_catalog import sync_operation_types

    sync_operation_types(frappe)
    if frappe.db.exists("Workspace", "Frappe Controller"):
        from frappe_controller.feature_flags import sync_workspace_visibility

        sync_workspace_visibility(frappe)
