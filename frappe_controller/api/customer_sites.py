"""Customer-form entry points for production-site provisioning."""

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


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        frappe.throw(f"{label} is required", frappe.ValidationError)
    return text


def _actor() -> str:
    actor = frappe.session.user
    if not actor or actor == "Guest" or not (_AUTHOR_ROLES & set(frappe.get_roles(actor))):
        frappe.throw("Controller Admin or Operator role is required", frappe.PermissionError)
    return actor


def _command_lifetime() -> int:
    value = frappe.conf.get("frappe_controller_command_lifetime_seconds", 300)
    if type(value) is not int or not 1 <= value <= 300:
        raise RuntimeError("invalid frappe_controller_command_lifetime_seconds configuration")
    return value


@frappe.whitelist(methods=["POST"])
def create_production_site(
    customer: str, domain: str, server_agent: str, bench: str
) -> Mapping[str, Any]:
    """Queue a blank production site and bind its future Managed Site name to Customer."""
    actor = _actor()
    customer = _text(customer, "Customer")
    if not frappe.db.exists("Customer", customer):
        frappe.throw("Customer does not exist", frappe.DoesNotExistError)
    if frappe.db.get_value("Customer", customer, "controller_production_managed_site"):
        frappe.throw(
            "Customer already has a production Managed Site; clear or replace it before requesting another",
            frappe.ValidationError,
        )
    server_agent = _text(server_agent, "Server Agent")
    bench = _text(bench, "Bench")

    try:
        payload = validate_lifecycle_payload("site.create_blank", {"domain": _text(domain, "Domain")}).normalized_payload
        if frappe.db.exists("Managed Site", {"domain": payload["domain"]}):
            frappe.throw("A Managed Site already exists for this domain", frappe.ValidationError)
        environment = target_environment(frappe, server_agent, None)
        if environment != "production":
            frappe.throw("Production sites require a production Server Agent", frappe.ValidationError)
        require_operation(
            frappe, environment=environment, operation_type="site.create_blank",
            payload_json=frappe.as_json(payload),
        )
        authored = OperationAuthoringService(
            FrappeOperationAuthoringRepository(frappe), validate_lifecycle_payload
        ).author(
            OperationRequest(
                operation_id=str(uuid4()), operation_type="site.create_blank",
                server_agent=server_agent, bench=bench, managed_site=None, payload=payload,
            ),
            actor=actor,
        )
        # Managed Site names are stable domains.  The link becomes resolvable when
        # the agent's next inventory heartbeat reports the newly created site.
        frappe.db.set_value(
            "Customer", customer, "controller_production_managed_site", payload["domain"],
            update_modified=False,
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


__all__ = ["create_production_site"]
