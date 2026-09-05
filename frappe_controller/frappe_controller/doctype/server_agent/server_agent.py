import ipaddress
import json
import re

import frappe
from frappe.model.document import Document

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields, prevent_delete, require_json, require_sha256,
)
from frappe_controller.controller_settings import load_controller_settings
from frappe_controller.frappe_security_store import FrappeCertificateStore

_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_FAILURES = {
    "deployment_failed", "health_timeout", "operator_requested", "version_mismatch",
}
_UPGRADE_FIELDS = (
    "drain_requested", "drain_requested_by", "drain_requested_at", "upgrade_state",
    "desired_agent_version", "desired_image_digest", "previous_agent_version",
    "previous_image_digest", "observed_image_digest", "upgrade_requested_by",
    "upgrade_requested_at", "upgrade_deployed_at", "upgrade_verified_at",
    "upgrade_failure_code",
)
_UPGRADE_TRANSITIONS = {
    "none": {"planned"},
    "planned": {"draining", "ready_to_deploy", "cancelled"},
    "draining": {"ready_to_deploy", "cancelled"},
    # A late lease from a transaction that began before drain persistence may
    # be observed after readiness.  Re-enter draining and never deploy over it.
    "ready_to_deploy": {"draining", "verifying", "rollback_required", "cancelled"},
    "verifying": {"succeeded", "rollback_required"},
    "succeeded": {"planned"},
    "rollback_required": {"rollback_verifying"},
    "rollback_verifying": {"rolled_back"},
    "rolled_back": {"planned"},
    "cancelled": {"planned"},
}


class ServerAgent(Document):
    def validate(self):
        if not _AGENT_ID.fullmatch(self.agent_id or ""):
            frappe.throw("Agent ID has an invalid format", frappe.ValidationError)
        if self.protocol_version != "1.0" or self.audience != "frappe-controller":
            frappe.throw("Unsupported controller protocol identity", frappe.ValidationError)
        if self.public_ip:
            try:
                normalized_ip = str(ipaddress.IPv4Address(self.public_ip))
            except ipaddress.AddressValueError:
                frappe.throw("Public IP must be a valid IPv4 address", frappe.ValidationError)
            if normalized_ip != self.public_ip:
                frappe.throw("Public IP must use canonical IPv4 notation", frappe.ValidationError)
        require_json(self.capabilities_json or "[]", "capabilities_json", list)
        suffixes = require_json(
            self.allowed_site_suffixes_json or "[]", "allowed_site_suffixes_json", list
        )
        operations = require_json(
            self.allowed_operations_json or "[]", "allowed_operations_json", list
        )
        if any(not isinstance(value, str) or not value for value in suffixes):
            frappe.throw("Allowed site suffixes must be non-empty strings", frappe.ValidationError)
        if any(not isinstance(value, str) or not value for value in operations):
            frappe.throw("Allowed operations must be non-empty strings", frappe.ValidationError)
        for field in ("enrollment_token_hash", "expected_public_key_sha256"):
            if self.get(field):
                require_sha256(self.get(field), field)
        if self.signing_public_key_ed25519:
            require_sha256(self.signing_public_key_ed25519, "signing_public_key_ed25519")
        if self.last_signed_request_at_ns and not re.fullmatch(
            r"[1-9][0-9]{0,19}", self.last_signed_request_at_ns
        ):
            frappe.throw("last_signed_request_at_ns is invalid", frappe.ValidationError)
        if not self.enabled:
            self.status = "Disabled"
        for field in (
            "desired_agent_version", "previous_agent_version",
        ):
            if self.get(field) and not _VERSION.fullmatch(self.get(field)):
                frappe.throw(f"{field} is invalid", frappe.ValidationError)
        for field in (
            "desired_image_digest", "previous_image_digest", "observed_image_digest",
        ):
            if self.get(field):
                require_sha256(self.get(field), field)
        if self.upgrade_failure_code and self.upgrade_failure_code not in _FAILURES:
            frappe.throw("upgrade_failure_code is invalid", frappe.ValidationError)
        active_upgrade = self.upgrade_state not in {"none", "succeeded", "rolled_back", "cancelled"}
        if active_upgrade and not (
            self.drain_requested and self.drain_requested_by and self.drain_requested_at
            and self.desired_agent_version and self.desired_image_digest
            and self.previous_agent_version and self.previous_image_digest
            and self.upgrade_requested_by and self.upgrade_requested_at
        ):
            frappe.throw("Active upgrade identity or drain attribution is incomplete", frappe.ValidationError)
        if self.upgrade_state == "verifying" and not (
            self.upgrade_deployed_at
            and self.observed_image_digest == self.desired_image_digest
        ):
            frappe.throw("Upgrade verification identity is incomplete", frappe.ValidationError)
        if self.upgrade_state == "rollback_verifying" and not (
            self.upgrade_deployed_at
            and self.observed_image_digest == self.previous_image_digest
        ):
            frappe.throw("Rollback verification identity is incomplete", frappe.ValidationError)
        terminal_identity = {
            "succeeded": (self.desired_agent_version, self.desired_image_digest),
            "rolled_back": (self.previous_agent_version, self.previous_image_digest),
        }
        if self.upgrade_state in terminal_identity:
            expected_version, expected_digest = terminal_identity[self.upgrade_state]
            if (
                self.drain_requested
                or not self.upgrade_verified_at
                or self.agent_version != expected_version
                or self.observed_image_digest != expected_digest
            ):
                frappe.throw("Terminal upgrade verification is incomplete", frappe.ValidationError)
        if self.upgrade_state == "cancelled" and (
            self.drain_requested
            or self.upgrade_deployed_at
            or self.upgrade_failure_code != "operator_requested"
        ):
            frappe.throw("Cancelled upgrade identity is invalid", frappe.ValidationError)
        previous = self.get_doc_before_save()
        if previous:
            changed = any(previous.get(field) != self.get(field) for field in _UPGRADE_FIELDS)
            if changed and not self.flags.get("controller_service"):
                frappe.throw("Upgrade fields are restricted to the controller service", frappe.PermissionError)
            if previous.upgrade_state != self.upgrade_state and self.upgrade_state not in _UPGRADE_TRANSITIONS.get(
                previous.upgrade_state, set()
            ):
                frappe.throw("Illegal agent upgrade state transition", frappe.ValidationError)
        elif (
            self.upgrade_state != "none"
            or any(self.get(field) for field in _UPGRADE_FIELDS if field != "upgrade_state")
        ) and not self.flags.get("controller_service"):
            frappe.throw("Upgrade fields are restricted to the controller service", frappe.PermissionError)
        immutable_fields(self, ("agent_id", "protocol_version", "audience"))

    def on_trash(self):
        prevent_delete("Server Agent")


@frappe.whitelist()
def generate_install_token(agent_id: str):
    """Return a short-lived enrollment token once to a Controller Admin."""
    user = getattr(getattr(frappe, "session", None), "user", None)
    if not user or user == "Guest" or "Controller Admin" not in frappe.get_roles(user):
        frappe.throw("Controller Admin role is required", frappe.PermissionError)
    agent = frappe.get_doc("Server Agent", agent_id)
    try:
        suffixes = json.loads(agent.allowed_site_suffixes_json or "[]")
        operations = json.loads(agent.allowed_operations_json or "[]")
    except (TypeError, json.JSONDecodeError):
        suffixes = operations = []
    if not isinstance(suffixes, list) or not suffixes or not isinstance(operations, list) or not operations:
        frappe.throw("Complete the Agent installation policy first", frappe.ValidationError)
    settings = load_controller_settings(frappe)
    if not settings.public_controller_url or not settings.agent_image_reference:
        frappe.throw(
            "Set Public Controller URL and Agent Image Reference in Frappe Controller Settings first",
            frappe.ValidationError,
        )
    token = FrappeCertificateStore.from_environment(
        frappe_module=frappe
    ).create_enrollment_token(
        agent.agent_id,
        actor=user,
        ttl_seconds=settings.enrollment_token_ttl_seconds,
    )
    return {
        "agent_id": agent.agent_id,
        "enrollment_token": token,
        "expires_in_seconds": settings.enrollment_token_ttl_seconds,
        "install_command": (
            "sudo ./setup.sh --controller "
            f"{settings.public_controller_url} --agent-id {agent.agent_id}"
        ),
    }
