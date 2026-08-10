import hashlib
import json

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields, prevent_delete, require_sha256,
)
from frappe_controller.feature_flags import require_operation, target_environment

_APPROVAL_FIELDS = (
    "operation", "approval_policy", "decision", "approver", "decided_at",
    "comment", "payload_hash_snapshot", "target_key_snapshot",
    "preview_result_hash_snapshot", "decision_hash",
)


class OperationApproval(Document):
    def before_insert(self):
        self.approver = frappe.session.user
        self.decided_at = now_datetime()
        operation = frappe.db.get_value(
            "Operation", self.operation,
            ["payload_hash", "preview_result_hash"], as_dict=True,
        )
        target_key = frappe.db.get_value(
            "Operation Target", {"operation": self.operation}, "target_key"
        )
        if not operation or not target_key:
            frappe.throw("Operation approval target does not exist", frappe.ValidationError)
        self.payload_hash_snapshot = operation.payload_hash
        self.target_key_snapshot = target_key
        self.preview_result_hash_snapshot = operation.preview_result_hash
        body = json.dumps(
            {"operation": self.operation, "policy": self.approval_policy,
             "decision": self.decision, "approver": self.approver,
             "decided_at": str(self.decided_at), "comment": self.comment or "",
             "payload_hash": self.payload_hash_snapshot,
             "target_key": self.target_key_snapshot,
             "preview_result_hash": self.preview_result_hash_snapshot},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        self.decision_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    def validate(self):
        require_sha256(self.decision_hash, "decision_hash")
        require_sha256(self.payload_hash_snapshot, "payload_hash_snapshot")
        if not isinstance(self.target_key_snapshot, str) or not self.target_key_snapshot:
            frappe.throw("Approval target key snapshot is invalid", frappe.ValidationError)
        if self.preview_result_hash_snapshot:
            require_sha256(self.preview_result_hash_snapshot, "preview_result_hash_snapshot")
        if "Approver" not in frappe.get_roles(self.approver):
            frappe.throw("Approver role is required", frappe.PermissionError)
        operation = frappe.db.get_value(
            "Operation", self.operation,
            [
                "requested_by", "approval_policy", "approval_status", "required_approvals",
                "payload_hash", "preview_result_hash", "server_agent",
                "operation_type", "payload_json", "bulk_parent",
                "managed_site",
            ],
            as_dict=True,
        )
        if not operation or operation.approval_policy != self.approval_policy:
            frappe.throw("Approval policy does not match the operation", frappe.ValidationError)
        if operation.requested_by == self.approver:
            frappe.throw("An operation requester cannot approve their own operation", frappe.PermissionError)
        require_operation(
            frappe,
            environment=target_environment(
                frappe, operation.server_agent, operation.managed_site
            ),
            operation_type=operation.operation_type,
            payload_json=operation.payload_json,
            bulk_parent=operation.bulk_parent,
        )
        if operation.approval_status != "pending":
            frappe.throw("Operation is not accepting approvals", frappe.ValidationError)
        target_key = frappe.db.get_value(
            "Operation Target", {"operation": self.operation}, "target_key"
        )
        if (
            operation.payload_hash != self.payload_hash_snapshot
            or operation.preview_result_hash != self.preview_result_hash_snapshot
            or target_key != self.target_key_snapshot
        ):
            frappe.throw("Approval snapshot does not match the operation", frappe.ValidationError)
        if frappe.db.exists("Operation Approval", {"operation": self.operation, "approver": self.approver}):
            frappe.throw("Each approver may decide only once", frappe.DuplicateEntryError)
        immutable_fields(self, _APPROVAL_FIELDS)

    def after_insert(self):
        if self.decision == "rejected":
            frappe.db.set_value("Operation", self.operation, {"approval_status": "rejected"}, update_modified=False)
            return
        approved = frappe.db.count("Operation Approval", {"operation": self.operation, "decision": "approved"})
        required = frappe.db.get_value("Operation", self.operation, "required_approvals") or 0
        values = {"approval_count": approved}
        if approved >= required:
            values["approval_status"] = "approved"
        frappe.db.set_value("Operation", self.operation, values, update_modified=False)

    def on_trash(self):
        prevent_delete("Operation Approval")
