"""Real Frappe retry/approval/Customer checks; always rolls back, never dispatches."""
import os
from uuid import uuid4
from unittest.mock import patch

import frappe


def main():
    frappe.init(site=os.environ["CONTROLLER_TEST_SITE"], sites_path=".")
    frappe.connect()
    frappe.set_user("Administrator")
    from frappe_controller.api.customer_sites import create_production_site
    from frappe_controller.api.operations import retry_operation

    before = {dt: frappe.db.count(dt) for dt in ["Customer", "Operation", "Operation Target", "Approval Policy"]}
    marker = uuid4().hex[:12]
    sample = frappe.db.get_value("Customer", {}, ["customer_group", "territory"], as_dict=True)

    def reject(source, **kwargs):
        try:
            retry_operation(source, **kwargs)
        except frappe.ValidationError:
            return
        raise AssertionError("Unsafe retry was accepted")

    try:
        with patch.object(frappe.db, "commit", side_effect=AssertionError("Test attempted a commit")):
            customer = frappe.get_doc({
                "doctype": "Customer", "customer_name": f"Retry regression {marker}",
                "customer_type": "Company", "customer_group": sample.customer_group, "territory": sample.territory,
            }).insert(ignore_permissions=True)
            created = create_production_site(customer.name, f"retry-{marker}.kaleam.net", "server-01", "dev-erp3")
            original_id = created["operation_id"]
            for state in ["queued", "leased", "running", "succeeded"]:
                frappe.db.set_value("Operation", original_id, "state", state)
                reject(original_id, recovery_confirmed=1)
            frappe.db.set_value("Operation", original_id, "state", "needs_intervention")
            reject(original_id)
            original = frappe.get_doc("Operation", original_id).as_dict()
            result = retry_operation(original_id, recovery_confirmed=1)
            assert result["operation_id"] != original_id and result["state"] == "queued"
            child = frappe.get_doc("Operation", result["operation_id"])
            assert child.retry_of == original_id
            assert frappe.db.get_value("Operation Target", {"operation": child.name}, "retry_of") == frappe.db.get_value("Operation Target", {"operation": original_id}, "name")
            assert child.payload_hash == original.payload_hash
            assert child.command_hash != original.command_hash
            customer.reload()
            assert customer.controller_site_creation_operation == child.name
            assert frappe.get_doc("Operation", original_id).as_dict() == original, "Original operation was modified"
            assert retry_operation(original_id, recovery_confirmed=1) == result, "Duplicate retry created another operation"

            frappe.db.set_value("Operation", child.name, "state", "failed")
            policy = frappe.get_doc({
                "doctype": "Approval Policy", "policy_name": f"Retry policy {marker}",
                "environment": "development", "operation_pattern": "site.create_blank",
                "minimum_approvals": 1, "require_backup": 0, "notify_approvers_by_email": 0,
            }).insert(ignore_permissions=True)
            next_result = retry_operation(child.name)
            next_child = frappe.get_doc("Operation", next_result["operation_id"])
            assert next_result["state"] == "awaiting_approval"
            assert next_child.approval_policy == policy.name and next_child.approval_count == 0
            assert next_child.retry_of == child.name
            assert not next_child.command_json
            customer.reload()
            assert customer.controller_site_creation_operation == next_child.name
            print("Real Frappe retry checks passed: immutable history, duplicate protection, Customer relink, fresh approvals, and unsafe-state rejection")
    finally:
        frappe.db.rollback()
        after = {dt: frappe.db.count(dt) for dt in before}
        assert before == after, (before, after)
        frappe.destroy()
        print("All retry test records rolled back; no jobs dispatched")


if __name__ == "__main__":
    main()
