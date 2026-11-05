"""Native dev-site audit/replay checks with the upstream writer mocked.

No source records are written. Temporary Integration Request logs are removed.
"""

import json
from unittest.mock import patch
from uuid import uuid4

import frappe

from frappe_controller.api import source_data


def verify():
    if not frappe.conf.developer_mode:
        raise RuntimeError("Source-data verification requires a development site")
    actor = frappe.session.user
    request_ids = [str(uuid4()) for _ in range(5)]
    evidence = {}
    try:
        frappe.set_user("Administrator")
        record = {"brand": "Controller verification only", "model": "Mocked addition"}
        with patch.object(source_data, "add_vehicle", return_value={"state": "added", "message": "Mocked"}) as write:
            first = source_data.add(record, request_ids[0])
            second = source_data.add(record, request_ids[0])
            assert first == second and write.call_count == 1
            log = frappe.get_doc("Integration Request", "controller-source-add-" + request_ids[0].replace("-", ""))
            assert log.status == "Completed" and json.loads(log.output)["state"] == "added"
            evidence["native_audit_and_replay"] = True
            try:
                source_data.add({**record, "model": "Different"}, request_ids[0])
            except frappe.ValidationError:
                evidence["request_payload_rebinding_denied"] = True
            else:
                raise AssertionError("Request ID was rebound")
        with patch.object(source_data, "add_vehicle", return_value={"state": "uncertain", "message": "Mocked timeout"}) as write:
            first = source_data.add(record, request_ids[1])
            second = source_data.add(record, request_ids[1])
            assert first["state"] == second["state"] == "uncertain" and write.call_count == 1
            evidence["uncertain_write_not_retried"] = True
        with patch.object(source_data, "add_vehicle", side_effect=source_data.SourceDataError("Mocked preflight failure")):
            assert source_data.add(record, request_ids[2])["state"] == "failed"
            evidence["preflight_failure_logged"] = True
        with patch.object(frappe, "get_roles", return_value=["Auditor"]), patch.object(source_data, "add_vehicle") as write:
            try:
                source_data.add(record, str(uuid4()))
            except frappe.PermissionError:
                evidence["read_role_write_denied"] = True
            else:
                raise AssertionError("Read-only role was allowed to write")
            write.assert_not_called()
        with patch.object(source_data, "delete_record", return_value={"state": "deleted", "message": "Mocked deletion"}) as remove:
            first = source_data.remove("colors", 1, "a" * 64, request_ids[3])
            second = source_data.remove("colors", 1, "a" * 64, request_ids[3])
            assert first == second and remove.call_count == 1
            evidence["native_delete_audit_and_replay"] = True
        with patch.object(frappe, "get_roles", return_value=["Auditor"]), patch.object(source_data, "delete_record") as remove:
            try:
                source_data.remove("colors", 1, "a" * 64, str(uuid4()))
            except frappe.PermissionError:
                evidence["read_role_delete_denied"] = True
            else:
                raise AssertionError("Read-only role was allowed to delete")
            remove.assert_not_called()
        with patch.object(source_data, "edit_record", return_value={"state": "updated", "message": "Mocked edit"}) as edit:
            first = source_data.edit("colors", 1, {"colorNameAR": "اختبار"}, "a" * 64, request_ids[4])
            second = source_data.edit("colors", 1, {"colorNameAR": "اختبار"}, "a" * 64, request_ids[4])
            assert first == second and edit.call_count == 1
            evidence["native_edit_audit_and_replay"] = True
        with patch.object(frappe, "get_roles", return_value=["Auditor"]), patch.object(source_data, "edit_record") as edit:
            try:
                source_data.edit("colors", 1, {"colorNameAR": "اختبار"}, "a" * 64, str(uuid4()))
            except frappe.PermissionError:
                evidence["read_role_edit_denied"] = True
            else:
                raise AssertionError("Read-only role was allowed to edit")
            edit.assert_not_called()
        evidence["live_source_writes"] = 0
        return evidence
    finally:
        frappe.set_user("Administrator")
        for request_id in request_ids:
            for action in ("add", "edit", "delete"):
                name = "controller-source-" + action + "-" + request_id.replace("-", "")
                if frappe.db.exists("Integration Request", name):
                    frappe.delete_doc("Integration Request", name, ignore_permissions=True, force=True)
        frappe.db.commit()
        frappe.set_user(actor)
