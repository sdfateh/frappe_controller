import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields, prevent_delete, require_json, require_sha256,
)

_EVENT_FIELDS = (
    "operation", "operation_id", "sequence", "attempt", "step", "kind",
    "details_json", "body_hash", "agent_created_at", "received_at",
)


class OperationEvent(Document):
    def before_insert(self):
        if not self.received_at:
            self.received_at = now_datetime()

    def validate(self):
        if self.sequence < 1 or (self.attempt is not None and self.attempt < 0):
            frappe.throw("Event sequence and attempt are invalid", frappe.ValidationError)
        if self.details_json is not None:
            require_json(self.details_json, "details_json")
        require_sha256(self.body_hash, "body_hash")
        operation = frappe.db.get_value(
            "Operation", self.operation, ["operation_id", "last_event_sequence"], as_dict=True
        )
        if not operation or operation.operation_id != self.operation_id:
            frappe.throw("Event operation identity does not match", frappe.ValidationError)
        duplicate = frappe.db.get_value(
            "Operation Event", {"operation": self.operation, "sequence": self.sequence},
            ["name", "body_hash"], as_dict=True,
        )
        if duplicate:
            frappe.throw(
                "Exact event duplicates must be resolved by the idempotent ingestion service"
                if duplicate.body_hash == self.body_hash else "Event sequence conflicts with immutable history",
                frappe.DuplicateEntryError if duplicate.body_hash == self.body_hash else frappe.ValidationError,
            )
        if self.is_new() and self.sequence != int(operation.last_event_sequence or 0) + 1:
            frappe.throw("Event sequence is not contiguous", frappe.ValidationError)
        immutable_fields(self, _EVENT_FIELDS)

    def after_insert(self):
        frappe.db.set_value("Operation", self.operation, "last_event_sequence", self.sequence, update_modified=False)

    def on_trash(self):
        prevent_delete("Operation Event")
