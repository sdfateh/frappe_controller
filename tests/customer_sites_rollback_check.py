"""Real Frappe regression check; all rows are rolled back, never dispatched.

Run using the bench Python from its sites directory, with CONTROLLER_TEST_SITE
set. Uses the enrolled development server-01/dev-erp3 but never commits work.
"""
import os
from uuid import uuid4
from unittest.mock import patch

import frappe


def main():
    frappe.init(site=os.environ["CONTROLLER_TEST_SITE"], sites_path=".")
    frappe.connect()
    frappe.set_user("Administrator")
    from frappe_controller.api.customer_sites import (
        create_production_site, reconcile_customer_site, reconcile_customer_sites,
    )
    before = {dt: frappe.db.count(dt) for dt in ["Customer", "Operation", "Operation Target", "Managed Site"]}
    sample = frappe.db.get_value("Customer", {}, ["customer_group", "territory"], as_dict=True)
    assert sample, "At least one Customer is needed for group/territory defaults"

    def inventory(domain):
        frappe.get_doc({
            "doctype": "Managed Site", "site_id": domain, "domain": domain,
            "server_agent": "server-01", "bench": "dev-erp3", "environment": "development",
            "status": "active", "inventory_updated_at": frappe.utils.now_datetime(),
        }).insert(ignore_permissions=True)

    try:
        # Guard against any accidental transaction commit in the tested path.
        with patch.object(frappe.db, "commit", side_effect=AssertionError("Test attempted a commit")):
            for inventory_first in [False, True]:
                marker = uuid4().hex[:12]
                domain = f"link-regression-{marker}.kaleam.net"
                customer = frappe.get_doc({
                    "doctype": "Customer", "customer_name": f"Link regression {marker}",
                    "customer_type": "Company", "customer_group": sample.customer_group,
                    "territory": sample.territory,
                }).insert(ignore_permissions=True)
                result = create_production_site(customer.name, domain, "server-01", "dev-erp3")
                assert result["state"] == "queued", result
                customer.reload()
                assert customer.controller_site_creation_operation == result["operation_id"]
                assert not customer.controller_production_managed_site
                assert not frappe.db.exists("Managed Site", domain)
                assert create_production_site(customer.name, domain, "server-01", "dev-erp3") == result
                if inventory_first:
                    inventory(domain)
                    assert not reconcile_customer_site(customer.name)
                frappe.db.set_value("Operation", result["operation_id"], "state", "succeeded")
                if not inventory_first:
                    assert not reconcile_customer_site(customer.name)
                    inventory(domain)
                reconcile_customer_sites()
                customer.reload()
                assert customer.controller_production_managed_site == domain
                assert not reconcile_customer_site(customer.name)
                print("Real Frappe link validation passed:", "inventory first" if inventory_first else "result first")
    finally:
        frappe.db.rollback()
        after = {dt: frappe.db.count(dt) for dt in before}
        assert before == after, (before, after)
        frappe.destroy()
        print("Rolled back all test Customers, operations, targets, and site inventory; no jobs dispatched")


if __name__ == "__main__":
    main()
