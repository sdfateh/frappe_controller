"""Reviewed enrollment defaults; selecting a profile never enables a feature."""

import json
import re


PROFILES = {
    "inventory": {"label": "Inventory and verification", "operations": ["site.verify"], "required_apps": ["frappe"]},
    "backup": {"label": "Backup and verification", "operations": ["site.verify", "site.backup"], "required_apps": ["frappe"]},
    "customer_erp": {
        "label": "Customer ERP creation",
        "operations": ["site.verify", "site.backup", "site.create_blank"],
        "required_apps": ["frappe", "erpnext", "mos_pro"],
    },
}


def required_apps(value: str | None) -> list[str]:
    apps = json.loads(value or '["frappe"]')
    if (not isinstance(apps, list) or not 1 <= len(apps) <= 32 or apps[0] != "frappe"
        or any(not isinstance(app, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", app) for app in apps)
        or len(set(apps)) != len(apps)):
        raise ValueError("Required apps must be unique app names starting with frappe")
    return apps
