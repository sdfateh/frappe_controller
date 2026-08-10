import frappe
from frappe.model.document import Document

from frappe_controller.frappe_controller.doctype._invariants import immutable_fields, prevent_delete

_POLICY_FIELDS = (
    "policy_name", "environment", "operation_pattern", "minimum_approvals",
    "require_distinct_approvers", "prohibit_requester_approval", "require_backup",
    "bulk_threshold", "maximum_targets",
)


class ApprovalPolicy(Document):
    def validate(self):
        if self.minimum_approvals < 1:
            frappe.throw("Approval policies require at least one approval", frappe.ValidationError)
        if self.bulk_threshold < 1 or self.maximum_targets < self.bulk_threshold:
            frappe.throw("Approval policy target limits are invalid", frappe.ValidationError)
        if self.get_doc_before_save() and frappe.db.exists("Operation", {"approval_policy": self.name}):
            immutable_fields(self, _POLICY_FIELDS)

    def on_trash(self):
        prevent_delete("Approval Policy")
