import re

import frappe
from frappe.model.document import Document

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields, prevent_delete, require_json, require_link_value,
)

_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class ManagedSite(Document):
    def validate(self):
        normalized = (self.domain or "").lower().rstrip(".")
        if normalized != self.domain or not _DOMAIN.fullmatch(normalized):
            frappe.throw("Domain must be a normalized FQDN", frappe.ValidationError)
        require_json(self.installed_apps_json or "[]", "installed_apps_json", list)
        labels = require_json(self.labels_json or "[]", "labels_json", list)
        if (
            len(labels) > 32
            or any(
                not isinstance(label, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", label)
                for label in labels
            )
            or len(set(labels)) != len(labels)
            or labels != sorted(labels)
        ):
            frappe.throw("Controller labels must be sorted, unique, and bounded", frappe.ValidationError)
        require_link_value("Bench", self.bench, "server_agent", self.server_agent)
        immutable_fields(self, ("site_id", "domain", "server_agent", "bench"))

    def on_trash(self):
        prevent_delete("Managed Site")
