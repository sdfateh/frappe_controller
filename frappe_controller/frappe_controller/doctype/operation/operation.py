import json

import frappe
from frappe.model.document import Document

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields, legal_transition, prevent_delete, require_json,
    require_link_value, require_sha256,
)

_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out", "needs_intervention", "dead_letter", "rejected"})
_TRANSITIONS = {
    "awaiting_approval": frozenset({"queued", "cancelled"}),
    "queued": frozenset({"leased", "failed", "cancelled", "timed_out"}),
    "leased": frozenset({"queued", "running", "cancelled", "timed_out"}),
    "running": frozenset({"queued", *_TERMINAL}),
    **{state: frozenset() for state in _TERMINAL},
}
_IDENTITY_FIELDS = (
    "operation_id", "idempotency_key", "protocol_version", "server_agent", "bench",
    "managed_site", "operation_type", "payload_json", "payload_hash", "requested_by",
    "approval_policy", "required_approvals", "command_json", "command_hash",
    "bulk_parent", "bulk_target", "retry_of",
    "preview_of", "preview_result_hash",
)
_CREDENTIAL_FIELDS = (
    "administrator_credential", "credential_received_at", "credential_consumed_at",
)


class Operation(Document):
    def before_insert(self):
        if not self.requested_by:
            self.requested_by = frappe.session.user
        elif frappe.session.user != "Administrator" and self.requested_by != frappe.session.user:
            frappe.throw("requested_by must be the authenticated user", frappe.PermissionError)

    def validate(self):
        if self.protocol_version != "1.0":
            frappe.throw("Unsupported operation protocol version", frappe.ValidationError)
        require_json(self.payload_json, "payload_json")
        require_sha256(self.payload_hash, "payload_hash")
        if self.command_json:
            require_json(self.command_json, "command_json")
        if self.command_hash:
            require_sha256(self.command_hash, "command_hash")
        if bool(self.preview_of) != bool(self.preview_result_hash):
            frappe.throw("Data update preview lineage is incomplete", frappe.ValidationError)
        if self.preview_result_hash:
            require_sha256(self.preview_result_hash, "preview_result_hash")
        if self.operation_type in {"data.update", "data.update.break_glass"}:
            try:
                command = json.loads(self.payload_json)
                payload = command["payload"]
                dry_run = payload["dry_run"]
            except (TypeError, KeyError, json.JSONDecodeError):
                frappe.throw("Data update operation payload is invalid", frappe.ValidationError)
            if (
                not isinstance(command, dict) or set(command) != {"policy_id", "payload"}
                or not isinstance(payload, dict) or type(dry_run) is not bool
            ):
                frappe.throw("Data update operation payload is invalid", frappe.ValidationError)
            if dry_run and self.preview_of:
                frappe.throw("A dry run cannot be promoted as an actual update", frappe.ValidationError)
            if not dry_run and not self.preview_of:
                frappe.throw("An actual data update requires a successful preview", frappe.PermissionError)
            if self.preview_of:
                other_promotion = frappe.db.exists(
                    "Operation",
                    {"preview_of": self.preview_of, "name": ["!=", self.name or ""]},
                )
                bulk_binding = frappe.db.get_value(
                    "Bulk Operation Target", {"preview_operation": self.preview_of}, "name"
                )
                if other_promotion or (
                    bulk_binding and bulk_binding != (self.bulk_target or "")
                ):
                    frappe.throw(
                        "A data update preview may authorize only one apply lineage",
                        frappe.DuplicateEntryError,
                    )
                preview = frappe.db.get_value(
                    "Operation", self.preview_of,
                    [
                        "operation_type", "server_agent", "bench", "managed_site",
                        "requested_by", "state", "result_hash", "payload_json",
                    ],
                    as_dict=True,
                )
                preview_payload = {}
                try:
                    preview_command = json.loads(preview.payload_json) if preview else None
                    preview_payload = preview_command["payload"]
                    expected_actual = {
                        **preview_command,
                        "payload": {**preview_payload, "dry_run": False},
                    }
                except (TypeError, KeyError, json.JSONDecodeError):
                    preview = None
                    expected_actual = None
                if (
                    not preview or preview.operation_type != self.operation_type
                    or preview.server_agent != self.server_agent or preview.bench != self.bench
                    or preview.managed_site != self.managed_site
                    or preview.requested_by != self.requested_by
                    or preview.state != "succeeded"
                    or preview.result_hash != self.preview_result_hash
                    or preview_payload.get("dry_run") is not True
                    or command != expected_actual
                ):
                    frappe.throw("Data update preview binding is invalid", frappe.PermissionError)
        elif self.preview_of or self.preview_result_hash:
            frappe.throw("Preview lineage is only valid for data updates", frappe.ValidationError)
        if self.state in {"queued", "leased", "running", *_TERMINAL} and not (self.command_json and self.command_hash):
            frappe.throw("Dispatched operation requires an immutable command snapshot", frappe.ValidationError)
        require_link_value("Bench", self.bench, "server_agent", self.server_agent)
        if self.managed_site:
            require_link_value("Managed Site", self.managed_site, "bench", self.bench)
            require_link_value("Managed Site", self.managed_site, "server_agent", self.server_agent)
        bulk_links = (self.bulk_parent, self.bulk_target, self.retry_of)
        if any(bulk_links):
            if not self.bulk_parent or not self.bulk_target or not self.managed_site:
                frappe.throw("Bulk child operation linkage is incomplete", frappe.ValidationError)
            target = frappe.db.get_value(
                "Bulk Operation Target",
                self.bulk_target,
                [
                    "bulk_operation", "server_agent", "bench", "managed_site",
                    "retry_of", "retry_source", "attempt",
                ],
                as_dict=True,
            )
            if (
                not target
                or target.bulk_operation != self.bulk_parent
                or target.server_agent != self.server_agent
                or target.bench != self.bench
                or target.managed_site != self.managed_site
            ):
                frappe.throw("Bulk child operation target does not match", frappe.ValidationError)
            if int(target.attempt or 0) == 0 and self.retry_of and not target.retry_source:
                frappe.throw("Initial bulk child cannot have retry lineage", frappe.ValidationError)
            if int(target.attempt or 0) > 0 or target.retry_source:
                lineage_target = target.retry_of or target.retry_source
                previous_target = frappe.db.get_value(
                    "Bulk Operation Target", lineage_target, "child_operation"
                )
                previous = frappe.db.get_value(
                    "Operation",
                    self.retry_of,
                    ["bulk_parent", "operation_type", "payload_hash", "state"],
                    as_dict=True,
                )
                if (
                    not self.retry_of
                    or previous_target != self.retry_of
                    or not previous
                    or (
                        previous.bulk_parent != self.bulk_parent
                        and previous.bulk_parent
                        != frappe.db.get_value("Bulk Operation", self.bulk_parent, "retry_of")
                    )
                    or previous.operation_type != self.operation_type
                    or previous.payload_hash != self.payload_hash
                    or previous.state not in {"failed", "timed_out", "needs_intervention", "dead_letter", "rejected"}
                ):
                    frappe.throw("Bulk child retry lineage is invalid", frappe.ValidationError)
        if self.required_approvals < 0 or self.approval_count < 0:
            frappe.throw("Approval counts cannot be negative", frappe.ValidationError)
        if self.state in {"queued", "leased"} and self.approval_status not in {"approved", "not_required"}:
            frappe.throw("Operation approval threshold is not satisfied", frappe.PermissionError)
        if self.approval_status == "not_required" and self.required_approvals:
            frappe.throw("Required approvals cannot be bypassed", frappe.PermissionError)
        immutable_fields(self, _IDENTITY_FIELDS)
        previous = self.get_doc_before_save()
        if previous and any(previous.get(field) != self.get(field) for field in _CREDENTIAL_FIELDS):
            if not self.flags.get("controller_service"):
                frappe.throw("Operation credential fields are service-owned", frappe.PermissionError)
        elif not previous and any(self.get(field) for field in _CREDENTIAL_FIELDS):
            if not self.flags.get("controller_service"):
                frappe.throw("Operation credential fields are service-owned", frappe.PermissionError)
        legal_transition(self, _TRANSITIONS)

    def on_trash(self):
        prevent_delete("Operation")
