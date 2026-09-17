"""Authenticated decisions for single-operation approval requests."""

from __future__ import annotations

from typing import Any, Mapping

import frappe


@frappe.whitelist(methods=["POST"])
def decide_operation(
    operation_id: str, decision: str, comment: str | None = None
) -> Mapping[str, Any]:
    actor = frappe.session.user
    if not actor or actor == "Guest" or "Approver" not in frappe.get_roles(actor):
        frappe.throw("Approver role is required", frappe.PermissionError)
    if decision not in {"approved", "rejected"}:
        frappe.throw("Decision must be approved or rejected", frappe.ValidationError)
    if comment is not None and (
        not isinstance(comment, str) or len(comment.strip()) > 1000
    ):
        frappe.throw("Approval comment is invalid", frappe.ValidationError)
    operation = frappe.db.get_value(
        "Operation",
        operation_id,
        ["name", "approval_policy", "approval_status", "requested_by"],
        as_dict=True,
    )
    if not operation:
        frappe.throw("Operation does not exist", frappe.DoesNotExistError)
    if operation.approval_status != "pending":
        frappe.throw("Operation is not accepting approvals", frappe.ValidationError)
    if operation.requested_by == actor:
        frappe.throw("An operation requester cannot approve their own operation", frappe.PermissionError)
    frappe.get_doc(
        {
            "doctype": "Operation Approval",
            "operation": operation.name,
            "approval_policy": operation.approval_policy,
            "decision": decision,
            "comment": (comment or "").strip(),
        }
    ).insert()
    updated = frappe.db.get_value(
        "Operation",
        operation.name,
        ["approval_status", "approval_count", "required_approvals"],
        as_dict=True,
    )
    return {
        "operation_id": operation.name,
        "approval_status": updated.approval_status,
        "approval_count": int(updated.approval_count or 0),
        "required_approvals": int(updated.required_approvals or 0),
    }


__all__ = ["decide_operation"]
