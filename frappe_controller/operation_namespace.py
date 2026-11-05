"""Move legacy Controller jobs out of ERPNext's manufacturing Operation DocType.

Runs before schema migration. It moves only rows carrying a Controller operation
identity and updates only references to those rows. Manufacturing rows and links
retain ERPNext's Operation name. DDL uses Frappe's migration transaction API;
the data move is idempotent across schema-stage commits and interrupted retries.
"""

from __future__ import annotations


def migrate():
    import frappe

    db = frappe.db
    if not db.table_exists("Operation") or "operation_id" not in db.get_table_columns("Operation"):
        return
    # Import the reviewed new schema before copying any legacy rows.
    frappe.reload_doc("frappe_controller", "doctype", "controller_operation", force=True)
    columns = sorted(set(db.get_table_columns("Operation")) & set(db.get_table_columns("Controller Operation")))
    assert {"name", "operation_id", "payload_hash"}.issubset(columns)
    legacy = "SELECT name FROM `tabOperation` WHERE IFNULL(operation_id,'')<>''"
    conflicts = db.sql(
        "SELECT o.name FROM `tabOperation` o JOIN `tabController Operation` n ON n.name=o.name "
        "WHERE IFNULL(o.operation_id,'')<>'' AND (o.operation_id<>n.operation_id OR o.payload_hash<>n.payload_hash) LIMIT 1"
    )
    if conflicts:
        raise RuntimeError("Controller operation namespace contains conflicting identities")
    rendered = ",".join("`" + field.replace("`", "``") + "`" for field in columns)
    selected = ",".join("o.`" + field.replace("`", "``") + "`" for field in columns)
    db.sql(f"INSERT INTO `tabController Operation` ({rendered}) SELECT {selected} FROM `tabOperation` o "
           "WHERE IFNULL(o.operation_id,'')<>'' AND NOT EXISTS (SELECT 1 FROM `tabController Operation` n WHERE n.name=o.name)")

    # Password fields are encrypted in __Auth, separately from document rows.
    if db.sql("SELECT 1 FROM `__Auth` o JOIN `__Auth` n ON n.name=o.name AND n.fieldname=o.fieldname "
              "WHERE o.doctype=%s AND n.doctype=%s AND o.name IN (" + legacy + ") "
              "AND (BINARY o.password<>BINARY n.password OR o.encrypted<>n.encrypted) LIMIT 1",
              ("Operation", "Controller Operation")):
        raise RuntimeError("Controller credential namespace contains conflicting values")
    db.sql("INSERT IGNORE INTO `__Auth` (doctype,name,fieldname,password,encrypted) "
           "SELECT %s,name,fieldname,password,encrypted FROM `__Auth` WHERE doctype=%s AND name IN (" + legacy + ")",
           ("Controller Operation", "Operation"))
    db.sql("DELETE FROM `__Auth` WHERE doctype=%s AND name IN (" + legacy + ")", ("Operation",))

    references = {
        "Comment": ("reference_doctype", "reference_name"),
        "Version": ("ref_doctype", "docname"),
        "File": ("attached_to_doctype", "attached_to_name"),
        "Communication": ("reference_doctype", "reference_name"),
        "DocShare": ("share_doctype", "share_name"),
        "ToDo": ("reference_type", "reference_name"),
        "Notification Log": ("document_type", "document_name"),
        "Activity Log": ("reference_doctype", "reference_name"),
        "User Permission": ("allow", "for_value"),
    }
    for doctype, (kind, name) in references.items():
        if db.table_exists(doctype) and {kind, name}.issubset(db.get_table_columns(doctype)):
            db.sql(f"UPDATE `tab{doctype}` SET `{kind}`=%s WHERE `{kind}`=%s AND `{name}` IN ({legacy})",
                   ("Controller Operation", "Operation"))
    db.sql("DELETE FROM `tabOperation` WHERE IFNULL(operation_id,'')<>''")

    # Legacy uniqueness constraints must not constrain manufacturing documents,
    # whose inserts no longer populate these Controller-only columns.
    indexes = db.sql("SHOW INDEX FROM `tabOperation`", as_dict=True)
    for index in {row["Key_name"] for row in indexes if row["Column_name"] in {"operation_id", "idempotency_key", "bulk_target"} and not row["Non_unique"]}:
        if index != "PRIMARY":
            db.sql_ddl("ALTER TABLE `tabOperation` DROP INDEX `" + index.replace("`", "``") + "`")
    frappe.reload_doc("manufacturing", "doctype", "operation", force=True)
    frappe.clear_cache()
