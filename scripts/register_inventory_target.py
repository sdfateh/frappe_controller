"""Register a disabled production target without enrolling an Agent or touching a site.

Run with the Controller's Bench Python from its sites directory. Pass the
repository-managed JSON profile with --profile; default is a read-only plan.
--apply commits all new records in one transaction. Existing records must match
their intended ownership and disabled state; this tool never repurposes them.
The deployment metadata in the profile is documentary, NOT executable routing
input for an Agent. Root-owned Agent/Helper policies remain a separate step.
"""

import argparse
import json
from pathlib import Path
import re


def registration_records(profile):
    required = {
        "schema_version", "controller_site", "agent_id", "server_name", "bench_id",
        "domain", "environment", "mode", "enabled", "allowed_operations",
        "target_compose_file", "target_backend_service", "target_frontend_service",
        "target_sites_volume", "target_container_sites_path",
    }
    if not isinstance(profile, dict) or set(profile) != required:
        raise ValueError("Invalid registration profile fields")
    if (
        type(profile["schema_version"]) is not int or profile["schema_version"] != 1
        or profile["environment"] != "production"
        or profile["mode"] != "registration_only"
        or profile["enabled"] is not False
        or profile["allowed_operations"] != []
    ):
        raise ValueError("This tool only registers disabled production targets without operations")
    for field in ("controller_site", "domain"):
        if not isinstance(profile[field], str) or not re.fullmatch(
            r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", profile[field]
        ):
            raise ValueError("Invalid site domain")
    for field in ("agent_id", "bench_id"):
        if not isinstance(profile[field], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", profile[field]):
            raise ValueError("Invalid Agent or Bench identity")
    if not isinstance(profile["server_name"], str) or not profile["server_name"].strip():
        raise ValueError("Server name is required")
    agent, bench, domain = (profile[key] for key in ("agent_id", "bench_id", "domain"))
    return [
        ("Server Agent", agent, {
            "agent_id": agent, "server_name": profile["server_name"],
            "environment": "production", "enabled": 0, "status": "Disabled",
            "protocol_version": "1.0", "audience": "frappe-controller",
            "allowed_site_suffixes_json": json.dumps([domain]),
            "allowed_operations_json": "[]", "capabilities_json": "[]",
        }),
        ("Bench", bench, {
            "bench_id": bench, "server_agent": agent, "display_name": bench,
            "environment": "production", "enabled": 0, "health_status": "unknown",
            "installed_apps_json": "[]", "capabilities_json": "[]",
        }),
        ("Managed Site", domain, {
            "site_id": domain, "domain": domain, "server_agent": agent,
            "bench": bench, "environment": "production", "status": "unknown",
            "health_status": "unknown", "installed_apps_json": "[]",
            "labels_json": '["inventory-pending","production"]',
        }),
    ]


def register(frappe, profile, *, apply=False):
    if frappe.session.user != "Administrator" and "Controller Admin" not in frappe.get_roles():
        raise PermissionError("Controller Admin is required")
    records = registration_records(profile)
    # Reject a preexisting alias rather than assigning the same domain twice.
    other_site = frappe.db.get_value("Managed Site", {"domain": profile["domain"]}, "name")
    if other_site and other_site != profile["domain"]:
        raise ValueError("Domain is already registered under a different identity")
    pending = []
    result = []
    for doctype, name, values in records:
        if frappe.db.exists(doctype, name):
            existing = frappe.get_doc(doctype, name)
            # A repeat is safe only before enrollment/inventory activation.
            # Never silently disable or steal an already-active target.
            for field, expected in values.items():
                actual = existing.get(field)
                if field.endswith("_json"):
                    actual, expected = json.loads(actual or "[]"), json.loads(expected)
                if actual != expected:
                    raise ValueError(f"Existing {doctype} {name} conflicts at {field}; no records changed")
            result.append({"doctype": doctype, "name": name, "action": "unchanged"})
        else:
            pending.append({"doctype": doctype, **values})
            result.append({"doctype": doctype, "name": name, "action": "create" if apply else "would_create"})
    if apply:
        for values in pending:
            frappe.get_doc(values).insert()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    profile = json.loads(Path(args.profile).read_text())
    registration_records(profile)  # Validate before connecting to any database.
    import frappe
    frappe.init(site=profile["controller_site"], sites_path=".")
    frappe.connect()
    try:
        frappe.set_user("Administrator")
        result = register(frappe, profile, apply=args.apply)
        if args.apply:
            frappe.db.commit()
        else:
            frappe.db.rollback()
        print(json.dumps({"mode": "registered_disabled" if args.apply else "plan", "records": result}))
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main()
