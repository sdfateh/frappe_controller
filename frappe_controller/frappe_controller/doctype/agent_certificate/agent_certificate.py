import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields, prevent_delete, require_sha256,
)

_TRANSITIONS = {
    "Pending": {"Active", "Revoked"},
    "Active": {"Rotating", "Revoked", "Expired"},
    "Rotating": {"Active", "Revoked", "Expired"},
    "Revoked": set(),
    "Expired": set(),
}


class AgentCertificate(Document):
    def validate(self):
        require_sha256(self.fingerprint_sha256, "fingerprint_sha256")
        require_sha256(self.public_key_sha256, "public_key_sha256")
        if get_datetime(self.valid_from) >= get_datetime(self.valid_until):
            frappe.throw("Certificate validity interval is invalid", frappe.ValidationError)
        previous = self.get_doc_before_save()
        if previous and previous.status != self.status:
            if self.status not in _TRANSITIONS.get(previous.status, set()):
                frappe.throw("Illegal certificate status transition", frappe.ValidationError)
        if self.status == "Revoked" and not (self.revoked_at and self.revoked_by and self.revocation_reason):
            frappe.throw("Revocation actor, time, and reason are required", frappe.ValidationError)
        immutable_fields(
            self,
            ("server_agent", "serial_number", "fingerprint_sha256", "public_key_sha256",
             "valid_from", "valid_until", "issued_at", "issued_by"),
        )

    def on_trash(self):
        prevent_delete("Agent Certificate")
