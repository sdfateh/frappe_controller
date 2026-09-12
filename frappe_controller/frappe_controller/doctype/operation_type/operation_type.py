import frappe
from frappe.model.document import Document

from frappe_controller.frappe_controller.doctype._invariants import immutable_fields, prevent_delete
from frappe_controller.operation_catalog import OPERATION_TYPE_KEYS


class OperationType(Document):
    def validate(self):
        if self.operation_type not in OPERATION_TYPE_KEYS:
            frappe.throw("Unsupported operation type", frappe.ValidationError)
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            frappe.throw("Display Name is required", frappe.ValidationError)
        self.display_name = self.display_name.strip()
        immutable_fields(self, ("operation_type", "category"))

    def on_trash(self):
        prevent_delete("Operation Type")
