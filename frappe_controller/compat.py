"""Frappe v16 Desk navigation and runtime helpers."""

from typing import Any


def desk_route(path: str) -> str:
    return "/desk/" + path.lstrip("/")


def in_test(frappe_module: Any) -> bool:
    return bool(getattr(frappe_module, "in_test", False))


def has_app_permission() -> bool:
    """Navigation obeys Controller role and deployment feature visibility gates."""
    import frappe
    from .feature_flags import runtime_feature_config

    actor = getattr(getattr(frappe, "session", None), "user", None)
    if not actor or actor == "Guest":
        return False
    roles = {"System Manager", "Controller Admin", "Operator", "Approver", "Auditor"}
    return bool(roles.intersection(frappe.get_roles(actor)) and runtime_feature_config(frappe).workspace_visible)
