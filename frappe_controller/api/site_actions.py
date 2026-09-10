"""Simple, safe authoring endpoint for single-site lifecycle actions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping
from uuid import uuid4

import frappe

from ..feature_flags import require_operation, target_environment
from ..frappe_operation_service import FrappeOperationAuthoringRepository
from ..frappe_store import FrappeCommandStore
from ..lifecycle_authoring import validate_lifecycle_payload
from ..operation_service import OperationAuthoringError, OperationAuthoringService, OperationRequest


_AUTHOR_ROLES = frozenset({"Controller Admin", "Operator"})
_ACTIONS = frozenset({
    "site.create_blank",
    "site.backup",
    "site.migrate",
    "site.scheduler.enable",
    "site.scheduler.disable",
    "site.maintenance.enable",
    "site.maintenance.disable",
})


def _actor() -> str:
    actor = frappe.session.user
    if not actor or actor == "Guest" or not (_AUTHOR_ROLES & set(frappe.get_roles(actor))):
        frappe.throw("Controller Admin or Operator role is required", frappe.PermissionError)
    return actor


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        frappe.throw(f"{label} is required", frappe.ValidationError)
    return text


def _command_lifetime() -> int:
    value = frappe.conf.get("frappe_controller_command_lifetime_seconds", 300)
    if type(value) is not int or not 1 <= value <= 300:
        raise RuntimeError("invalid frappe_controller_command_lifetime_seconds configuration")
    return value


@frappe.whitelist(methods=["POST"])
def submit_site_action(
    domain: str, server_agent: str, bench: str, action: str
) -> Mapping[str, Any]:
    """Author and queue a safe action without exposing protocol fields in Desk."""
    actor = _actor()
    domain = _text(domain, "Domain").lower().rstrip(".")
    server_agent = _text(server_agent, "Server Agent")
    bench = _text(bench, "Bench")
    action = _text(action, "Action")
    if action not in _ACTIONS:
        frappe.throw("Unsupported site action", frappe.ValidationError)

    managed_site = frappe.db.get_value("Managed Site", {"domain": domain}, "name")
    if action == "site.create_blank":
        if managed_site:
            frappe.throw("A Managed Site already exists for this domain", frappe.ValidationError)
        target_site = None
        payload: dict[str, Any] = {"domain": domain}
    else:
        if not managed_site:
            frappe.throw("Create the site first, then use this action", frappe.DoesNotExistError)
        target_site = managed_site
        payload = {"domain": domain}
        if action == "site.backup":
            payload["with_files"] = True

    try:
        require_operation(
            frappe,
            environment=target_environment(frappe, server_agent, target_site),
            operation_type=action,
            payload_json=frappe.as_json(payload),
        )
        authored = OperationAuthoringService(
            FrappeOperationAuthoringRepository(frappe), validate_lifecycle_payload
        ).author(
            OperationRequest(
                operation_id=str(uuid4()), operation_type=action,
                server_agent=server_agent, bench=bench,
                managed_site=target_site, payload=payload,
            ),
            actor=actor,
        )
        state = authored.state
        if authored.required_approvals == 0:
            FrappeCommandStore(
                frappe, command_lifetime_seconds=_command_lifetime()
            ).enqueue_approved_operation(authored.operation_id, now=datetime.now(UTC))
            state = "queued"
        return {"operation_id": authored.operation_id, "state": state}
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)
