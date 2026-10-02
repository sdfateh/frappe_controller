"""Customer-form site provisioning on any policy-enabled target environment."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any, Mapping
from uuid import uuid4

import frappe

from ..feature_flags import require_operation, target_environment
from ..creation_readiness import check_creation_readiness, creation_handover_ready
from ..frappe_operation_service import FrappeOperationAuthoringRepository
from ..frappe_store import FrappeCommandStore
from ..lifecycle_authoring import validate_lifecycle_payload
from ..operation_service import OperationAuthoringError, OperationAuthoringService, OperationRequest


_AUTHOR_ROLES = frozenset({"Controller Admin", "Operator"})
_MANAGED_SITE_FIELD = "controller_production_managed_site"
_SITE_OPERATION_FIELD = "controller_site_creation_operation"
_RECONCILE_CURSOR_KEY = "controller_customer_site_reconcile_cursor"
_RECONCILE_BATCH_SIZE = 100


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


def protect_managed_site_link(doc: Any, _method: str | None = None) -> None:
    """Reject Customer link changes outside controller-owned service functions."""
    previous = doc.get_doc_before_save()
    for field in (_MANAGED_SITE_FIELD, _SITE_OPERATION_FIELD):
        old_value = previous.get(field) if previous else None
        if old_value != doc.get(field) and not doc.flags.get("controller_managed_site_update"):
            frappe.throw("Site links are controlled by Frappe Controller", frappe.PermissionError)


def _lock_customer(customer: str) -> Any:
    """Serialize duplicate requests and reconciliation within the request transaction."""
    rows = frappe.db.sql("SELECT name FROM `tabCustomer` WHERE name=%s FOR UPDATE", (customer,))
    if not rows:
        frappe.throw("Customer does not exist", frappe.DoesNotExistError)
    return frappe.get_doc("Customer", customer)


def _set_site_operation(customer_doc: Any, operation_id: str) -> None:
    """Link to the already-inserted Operation, never to a future Managed Site."""
    customer_doc.flags.controller_managed_site_update = True
    customer_doc.set(_SITE_OPERATION_FIELD, operation_id)
    customer_doc.save(ignore_permissions=True)


def reconcile_customer_site(customer: str) -> bool:
    """Link only a successful creation confirmed by matching Agent/Bench inventory.

    Called periodically so result-before-inventory and inventory-before-result
    are both handled. No commits: the scheduler owns the transaction.
    """
    customer_doc = _lock_customer(customer)
    if customer_doc.get(_MANAGED_SITE_FIELD) or not customer_doc.get(_SITE_OPERATION_FIELD):
        return False
    operation = frappe.get_doc("Operation", customer_doc.get(_SITE_OPERATION_FIELD))
    if operation.operation_type != "site.create_blank" or operation.state != "succeeded":
        return False
    if not creation_handover_ready(operation.result_json, operation.credential_received_at):
        return False
    domain = json.loads(operation.payload_json).get("domain")
    site = frappe.db.get_value(
        "Managed Site", {"domain": domain},
        ["name", "server_agent", "bench", "status", "inventory_updated_at"], as_dict=True,
    )
    if not site or (
        site.server_agent != operation.server_agent or site.bench != operation.bench
        or site.status not in {"active", "maintenance"} or not site.inventory_updated_at
    ):
        return False
    customer_doc.flags.controller_managed_site_update = True
    customer_doc.set(_MANAGED_SITE_FIELD, site.name)
    customer_doc.save(ignore_permissions=True)
    return True


def reconcile_customer_sites() -> None:
    """Rotate bounded batches so permanently ineligible history cannot starve new sites.

    The site-scoped cache cursor is only a scan hint, never readiness evidence.
    Cache loss or a rolled-back job safely repeats a sweep; each Customer is
    locked and revalidated before linking.
    """
    cache = frappe.cache()
    cursor = cache.get_value(_RECONCILE_CURSOR_KEY) or ""
    candidates = frappe.db.sql(
        "SELECT c.name FROM `tabCustomer` c "
        "JOIN `tabOperation` o ON o.name=c.controller_site_creation_operation "
        "JOIN `tabManaged Site` s ON s.domain=JSON_UNQUOTE(JSON_EXTRACT(o.payload_json,'$.domain')) "
        "AND s.server_agent=o.server_agent AND s.bench=o.bench "
        "WHERE IFNULL(c.controller_production_managed_site,'')='' "
        "AND o.operation_type='site.create_blank' AND o.state='succeeded' "
        "AND s.status IN ('active','maintenance') AND s.inventory_updated_at IS NOT NULL "
        "AND c.name > %s ORDER BY c.name LIMIT %s",
        (cursor, _RECONCILE_BATCH_SIZE),
        as_dict=True,
    )
    for row in candidates:
        reconcile_customer_site(row.name)
    cache.set_value(
        _RECONCILE_CURSOR_KEY,
        candidates[-1].name if len(candidates) == _RECONCILE_BATCH_SIZE else "",
    )


@frappe.whitelist(methods=["POST"])
def check_site_readiness(customer: str, domain: str, server_agent: str, bench: str) -> Mapping[str, Any]:
    """Read-only preview. Authoring repeats it, so a UI check cannot bypass gates."""
    _actor()
    document = frappe.get_doc("Customer", _text(customer, "Customer"))
    document.check_permission("write")
    try:
        payload = validate_lifecycle_payload("site.create_blank", {"domain": _text(domain, "Domain")}).normalized_payload
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)
    return check_creation_readiness(frappe, domain=payload["domain"], server_agent=_text(server_agent, "Server Agent"), bench=_text(bench, "Bench"))


@frappe.whitelist(methods=["POST"])
def create_production_site(
    customer: str, domain: str, server_agent: str, bench: str
) -> Mapping[str, Any]:
    """Queue a blank site on the selected target and link it to the Customer.

    Keep the historical endpoint and field names for compatibility. The target's
    environment selects feature and approval policy, not Customer eligibility.
    """
    actor = _actor()
    customer = _text(customer, "Customer")
    customer_doc = _lock_customer(customer)
    customer_doc.check_permission("write")
    if customer_doc.get(_MANAGED_SITE_FIELD):
        frappe.throw(
            "Customer already has a Managed Site; clear or replace it before requesting another",
            frappe.ValidationError,
        )
    server_agent = _text(server_agent, "Server Agent")
    bench = _text(bench, "Bench")

    try:
        payload = validate_lifecycle_payload("site.create_blank", {"domain": _text(domain, "Domain")}).normalized_payload
        if customer_doc.get(_SITE_OPERATION_FIELD):
            existing = frappe.get_doc("Operation", customer_doc.get(_SITE_OPERATION_FIELD))
            if (
                existing.operation_type == "site.create_blank"
                and existing.server_agent == server_agent and existing.bench == bench
                and json.loads(existing.payload_json).get("domain") == payload["domain"]
            ):
                return {"operation_id": existing.name, "state": existing.state}
            frappe.throw(
                "Customer already has a site creation operation; review it before requesting another",
                frappe.ValidationError,
            )
        if frappe.db.exists("Managed Site", {"domain": payload["domain"]}):
            frappe.throw("A Managed Site already exists for this domain", frappe.ValidationError)
        environment = target_environment(frappe, server_agent, None)
        require_operation(
            frappe, environment=environment, operation_type="site.create_blank",
            payload_json=frappe.as_json(payload),
        )
        readiness = check_creation_readiness(frappe, domain=payload["domain"], server_agent=server_agent, bench=bench)
        if not readiness["ready"]:
            failures = "; ".join(row["label"] + ": " + row["message"] for row in readiness["checks"] if not row["passed"])
            frappe.throw("Site creation readiness failed: " + failures, frappe.ValidationError)
        authored = OperationAuthoringService(
            FrappeOperationAuthoringRepository(frappe), validate_lifecycle_payload
        ).author(
            OperationRequest(
                operation_id=str(uuid4()), operation_type="site.create_blank",
                server_agent=server_agent, bench=bench, managed_site=None, payload=payload,
            ),
            actor=actor,
        )
        _set_site_operation(customer_doc, authored.operation_id)
        state = authored.state
        if authored.required_approvals == 0:
            FrappeCommandStore(
                frappe, command_lifetime_seconds=_command_lifetime()
            ).enqueue_approved_operation(authored.operation_id, now=datetime.now(UTC))
            state = "queued"
        return {"operation_id": authored.operation_id, "state": state}
    except OperationAuthoringError as exc:
        frappe.throw(str(exc), frappe.ValidationError)


__all__ = [
    "create_production_site", "check_site_readiness", "protect_managed_site_link",
    "reconcile_customer_site", "reconcile_customer_sites",
]
