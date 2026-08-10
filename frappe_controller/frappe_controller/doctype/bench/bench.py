import frappe
from frappe.model.document import Document

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields, prevent_delete, require_json,
)


class Bench(Document):
    def validate(self):
        require_json(self.installed_apps_json or "[]", "installed_apps_json", list)
        require_json(self.capabilities_json or "[]", "capabilities_json", list)
        if not frappe.db.exists("Server Agent", self.server_agent):
            frappe.throw("Unknown owning Server Agent", frappe.ValidationError)
        immutable_fields(self, ("bench_id", "server_agent"))

    def on_trash(self):
        prevent_delete("Bench")
