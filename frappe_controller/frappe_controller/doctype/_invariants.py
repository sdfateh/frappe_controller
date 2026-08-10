"""Shared fail-closed invariants for controller DocType classes."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

import frappe
from frappe import _

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def immutable_fields(document, fields: tuple[str, ...]) -> None:
    previous = document.get_doc_before_save()
    if not previous:
        return
    for field in fields:
        if previous.get(field) != document.get(field):
            frappe.throw(_("{0} is immutable").format(field), frappe.ValidationError)


def prevent_delete(label: str) -> None:
    frappe.throw(_("{0} records are retained and cannot be deleted").format(label), frappe.PermissionError)


def require_sha256(value: str | None, field: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        frappe.throw(_("{0} must be a lowercase SHA-256 digest").format(field), frappe.ValidationError)


def require_json(value: str | None, field: str, expected_type: type = Mapping):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        frappe.throw(_("{0} must contain valid JSON").format(field), frappe.ValidationError)
    if not isinstance(parsed, expected_type):
        frappe.throw(_("{0} has the wrong JSON shape").format(field), frappe.ValidationError)
    return parsed


def legal_transition(document, transitions: Mapping[str, frozenset[str]]) -> None:
    previous = document.get_doc_before_save()
    if not previous or previous.state == document.state:
        return
    if document.state not in transitions.get(previous.state, frozenset()):
        frappe.throw(
            _("Illegal state transition: {0} to {1}").format(previous.state, document.state),
            frappe.ValidationError,
        )


def require_link_value(doctype: str, name: str | None, field: str, expected) -> None:
    if not name or frappe.db.get_value(doctype, name, field) != expected:
        frappe.throw(_("Linked ownership does not match").format(field), frappe.ValidationError)
