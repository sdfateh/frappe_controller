"""Read-only handover evidence for the Operation timeline; never return secrets."""

from __future__ import annotations

import frappe

from ..creation_readiness import creation_handover_ready


@frappe.whitelist()
def get_site_handover_status(operation_id: str) -> dict:
    operation = frappe.get_doc("Operation", operation_id)
    operation.check_permission("read")
    if operation.operation_type not in {"site.create", "site.create_blank", "site.create_from_backup"}:
        return {"applicable": False}
    customers = frappe.get_all("Customer", filters={
        "controller_site_creation_operation": operation.name,
    }, fields=["name", "controller_production_managed_site"], limit_page_length=2)
    for row in customers:
        frappe.get_doc("Customer", row.name).check_permission("read")
    customer_linked = bool(customers) and all(row.controller_production_managed_site for row in customers)
    required_apps_json = frappe.db.get_value("Bench", operation.bench, "required_apps_json")
    verified = operation.state == "succeeded" and creation_handover_ready(
        operation.result_json, operation.credential_received_at, required_apps_json or "[]",
    )
    return {
        "applicable": True,
        "customer_required": bool(customers),
        "customer_linked": bool(customer_linked),
        "ready": bool(verified and (not customers or customer_linked)),
    }
