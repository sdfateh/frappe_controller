"""Authenticated low-cardinality controller metric snapshot."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

import frappe

from ..frappe_controller_metrics import controller_metrics_snapshot


@frappe.whitelist(methods=["GET"])
def controller_metrics() -> Mapping[str, Any]:
    actor = frappe.session.user
    roles = set(frappe.get_roles(actor)) if actor and actor != "Guest" else set()
    if not ({"Controller Admin", "Auditor"} & roles):
        frappe.throw("Controller Admin or Auditor role is required", frappe.PermissionError)
    snapshot = controller_metrics_snapshot(frappe)
    frappe.local.response["headers"] = {"Cache-Control": "no-store"}
    return asdict(snapshot)


__all__ = ["controller_metrics"]
