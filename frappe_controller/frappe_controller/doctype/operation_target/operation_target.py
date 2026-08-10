import frappe
from frappe.model.document import Document

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields, legal_transition, prevent_delete, require_link_value, require_sha256,
)
from frappe_controller.frappe_controller.doctype.operation.operation import _TRANSITIONS


class OperationTarget(Document):
    def validate(self):
        require_sha256(self.payload_hash_snapshot, "payload_hash_snapshot")
        if self.inventory_revision_snapshot:
            require_sha256(self.inventory_revision_snapshot, "inventory_revision_snapshot")
        require_link_value("Bench", self.bench, "server_agent", self.server_agent)
        if self.managed_site:
            require_link_value("Managed Site", self.managed_site, "bench", self.bench)
        parent = frappe.db.get_value(
            "Operation", self.operation,
            [
                "server_agent", "bench", "managed_site", "operation_type", "payload_hash",
                "bulk_parent", "bulk_target", "retry_of",
            ],
            as_dict=True,
        )
        expected = (self.server_agent, self.bench, self.managed_site, self.operation_type_snapshot, self.payload_hash_snapshot)
        if not parent or tuple(parent.get(key) for key in ("server_agent", "bench", "managed_site", "operation_type", "payload_hash")) != expected:
            frappe.throw("Operation Target snapshot differs from its parent", frappe.ValidationError)
        parent_bulk = (parent.get("bulk_parent"), parent.get("bulk_target"), parent.get("retry_of"))
        target_bulk = (self.bulk_parent, self.bulk_target, None)
        if any(parent_bulk) or any(target_bulk) or self.retry_of:
            if not self.bulk_parent or not self.bulk_target:
                frappe.throw("Bulk Operation Target linkage is incomplete", frappe.ValidationError)
            if self.bulk_parent != parent.get("bulk_parent") or self.bulk_target != parent.get("bulk_target"):
                frappe.throw("Operation Target bulk linkage differs from Operation", frappe.ValidationError)
            previous_operation_target = None
            if parent.get("retry_of"):
                previous_operation_target = frappe.db.get_value(
                    "Operation Target", {"operation": parent.get("retry_of")}, "name"
                )
            if self.retry_of != previous_operation_target:
                frappe.throw("Operation Target retry lineage differs from Operation", frappe.ValidationError)
        immutable_fields(
            self,
            ("operation", "target_key", "server_agent", "bench", "managed_site",
             "site_domain_snapshot", "operation_type_snapshot", "payload_hash_snapshot",
             "inventory_revision_snapshot",
             "bulk_parent", "bulk_target", "retry_of"),
        )
        legal_transition(self, _TRANSITIONS)

    def on_trash(self):
        prevent_delete("Operation Target")
