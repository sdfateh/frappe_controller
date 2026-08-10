from __future__ import annotations

import hashlib
import json

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields,
    prevent_delete,
    require_sha256,
)

_IMMUTABLE = (
    "bulk_operation",
    "approval_policy",
    "decision",
    "approver",
    "decided_at",
    "comment",
    "snapshot_hash",
    "payload_hash",
    "dry_run_hash",
    "decision_hash",
)


class BulkOperationApproval(Document):
    def before_insert(self):
        if not self.flags.get("controller_service"):
            frappe.throw(
                "Bulk Operation Approval creation is restricted to the controller service",
                frappe.PermissionError,
            )
        self.approver = frappe.session.user
        self.decided_at = now_datetime()
        parent = frappe.db.get_value(
            "Bulk Operation",
            self.bulk_operation,
            ["snapshot_hash", "payload_hash", "dry_run_hash"],
            as_dict=True,
        )
        if not parent:
            frappe.throw("Bulk Operation does not exist", frappe.ValidationError)
        self.snapshot_hash = parent.snapshot_hash
        self.payload_hash = parent.payload_hash
        self.dry_run_hash = parent.dry_run_hash
        body = json.dumps(
            {
                "bulk_operation": self.bulk_operation,
                "approval_policy": self.approval_policy,
                "decision": self.decision,
                "approver": self.approver,
                "decided_at": str(self.decided_at),
                "comment": self.comment or "",
                "snapshot_hash": self.snapshot_hash,
                "payload_hash": self.payload_hash,
                "dry_run_hash": self.dry_run_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self.decision_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    def validate(self):
        for field in ("snapshot_hash", "payload_hash", "dry_run_hash", "decision_hash"):
            require_sha256(self.get(field), field)
        if "Approver" not in frappe.get_roles(self.approver):
            frappe.throw("Approver role is required", frappe.PermissionError)
        parent = frappe.db.get_value(
            "Bulk Operation",
            self.bulk_operation,
            [
                "requested_by",
                "approval_policy",
                "approval_status",
                "state",
                "snapshot_hash",
                "payload_hash",
                "dry_run_hash",
            ],
            as_dict=True,
        )
        if not parent or parent.approval_policy != self.approval_policy:
            frappe.throw("Approval policy does not match Bulk Operation", frappe.ValidationError)
        if parent.requested_by == self.approver:
            frappe.throw("Bulk operation requester cannot approve their own request", frappe.PermissionError)
        if parent.approval_status != "pending" or parent.state != "awaiting_approval":
            frappe.throw("Bulk Operation is not accepting approvals", frappe.ValidationError)
        if (
            parent.snapshot_hash != self.snapshot_hash
            or parent.payload_hash != self.payload_hash
            or parent.dry_run_hash != self.dry_run_hash
        ):
            frappe.throw("Bulk approval evidence binding changed", frappe.ValidationError)
        if frappe.db.exists(
            "Bulk Operation Approval",
            {"bulk_operation": self.bulk_operation, "approver": self.approver},
        ):
            frappe.throw("Each approver may decide only once", frappe.DuplicateEntryError)
        immutable_fields(self, _IMMUTABLE)

    def after_insert(self):
        if self.decision == "rejected":
            frappe.db.set_value(
                "Bulk Operation",
                self.bulk_operation,
                {"approval_status": "rejected", "state": "rejected"},
                update_modified=False,
            )
            return
        approved = frappe.db.count(
            "Bulk Operation Approval",
            {"bulk_operation": self.bulk_operation, "decision": "approved"},
        )
        required = int(
            frappe.db.get_value("Bulk Operation", self.bulk_operation, "required_approvals")
            or 0
        )
        values = {"approval_count": approved}
        if approved >= required:
            values.update(
                {"approval_status": "approved", "state": "approved", "approved_at": now_datetime()}
            )
        frappe.db.set_value(
            "Bulk Operation", self.bulk_operation, values, update_modified=False
        )

    def on_trash(self):
        prevent_delete("Bulk Operation Approval")
