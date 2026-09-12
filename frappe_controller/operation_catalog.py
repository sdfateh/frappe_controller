"""Source-owned catalog of operation protocol keys and default Desk names."""

from __future__ import annotations


OPERATION_TYPES = (
    ("site.create", "Create Site (Compatibility)", "Site Lifecycle"),
    ("site.create_blank", "Create Blank Site", "Site Lifecycle"),
    ("site.create_from_backup", "Create Site from Backup", "Site Lifecycle"),
    ("site.backup", "Back Up Site", "Backup and Recovery"),
    ("site.verify", "Verify Site", "Backup and Recovery"),
    ("site.restore", "Restore Site", "Backup and Recovery"),
    ("site.reinstall", "Reinstall Site", "Site Lifecycle"),
    ("site.delete", "Delete Site", "Site Lifecycle"),
    ("site.migrate", "Migrate Site", "Site Lifecycle"),
    ("site.scheduler.enable", "Enable Scheduler", "Routine Operations"),
    ("site.scheduler.disable", "Disable Scheduler", "Routine Operations"),
    ("site.maintenance.enable", "Enable Maintenance Mode", "Routine Operations"),
    ("site.maintenance.disable", "Disable Maintenance Mode", "Routine Operations"),
    ("site.config.update", "Update Site Configuration", "Routine Operations"),
    ("site.config.set", "Set Site Configuration (Compatibility)", "Routine Operations"),
    ("data.update", "Update Site Data", "Data Updates"),
    ("data.update.break_glass", "Emergency Site Data Update", "Data Updates"),
    ("operation.cancel", "Cancel Operation", "Operation Control"),
)

OPERATION_TYPE_KEYS = frozenset(key for key, _label, _category in OPERATION_TYPES)


def sync_operation_types(frappe_module=None) -> None:
    """Insert missing catalog rows without overwriting administrator display names."""
    if frappe_module is None:
        import frappe as frappe_module
    if not frappe_module.db.exists("DocType", "Operation Type"):
        return
    for operation_type, display_name, category in OPERATION_TYPES:
        if not frappe_module.db.exists("Operation Type", operation_type):
            frappe_module.get_doc(
                {
                    "doctype": "Operation Type",
                    "operation_type": operation_type,
                    "display_name": display_name,
                    "category": category,
                }
            ).insert(ignore_permissions=True)


__all__ = ["OPERATION_TYPES", "OPERATION_TYPE_KEYS", "sync_operation_types"]
