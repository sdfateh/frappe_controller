"""Native settings, request scoping and SSRF checks; no catalog writes."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import frappe
import requests
from urllib3.connection import HTTPSConnection

from frappe_controller.api import source_data as api
from frappe_controller.source_data import SourceDataError, get_api_origin, snapshot, source_api_context


def verify():
    if not frappe.conf.developer_mode:
        raise RuntimeError("Configuration verification requires a development site")
    actor = frappe.session.user
    evidence = {}
    try:
        frappe.set_user("Administrator")
        settings = frappe.get_single("Frappe Controller Settings")
        assert settings.meta.has_field("source_data_api_url")
        current = settings.source_data_api_url
        for invalid in ("http://api.example.com", "https://127.0.0.1", "https://api.example.com/tires"):
            settings.source_data_api_url = invalid
            try:
                settings.validate()
            except frappe.ValidationError:
                pass
            else:
                raise AssertionError("Unsafe origin accepted")
        settings.source_data_api_url = current
        evidence["native_settings_validation"] = True
        permissions = settings.meta.permissions
        assert all(row.role == "Controller Admin" for row in permissions if row.write)
        evidence["settings_write_requires_controller_admin"] = True

        other = "https://verify-" + uuid4().hex + ".example.invalid"
        with patch.object(frappe.db, "get_single_value", return_value=other), patch.object(api, "fetch_records", return_value=[{"brand": "Test"}]) as fetch:
            result = api.browse()
            assert result["source"] == other and fetch.call_count == 1
            with patch.object(api, "add_vehicle") as write:
                try:
                    api.add({"brand": "Test", "model": "Test"}, str(uuid4()), source="https://old.example.com")
                except frappe.ValidationError:
                    pass
                else:
                    raise AssertionError("A stale form was accepted")
                write.assert_not_called()
        evidence["configured_origin_and_stale_review_guard"] = True
        with source_api_context(other):
            assert snapshot({"id": 1}) != _default_snapshot({"id": 1})
        evidence["snapshots_are_bound_to_origin"] = True

        adapter = api._guarded_adapter_factory()(max_retries=0)
        assert adapter.max_retries.total == 0
        request = requests.Request("GET", "https://127.0.0.1/tires/vehicle/brand/list").prepare()
        with patch("socket.socket", side_effect=AssertionError("Network connection was attempted")):
            try:
                adapter.send(request)
            except SourceDataError:
                pass
            else:
                raise AssertionError("Internal address was permitted")
        evidence["private_address_blocked_before_connection"] = True
        connection = adapter.poolmanager.connection_from_host("api.example.com", port=443, scheme="https").ConnectionCls("api.example.com", port=443)
        sock = MagicMock()
        sock.getpeername.return_value = ("127.0.0.1", 443)
        from frappe.utils.safe_exec import BlockedRequest
        with patch.object(HTTPSConnection, "_new_conn", return_value=sock):
            try:
                connection._new_conn()
            except BlockedRequest:
                pass
            else:
                raise AssertionError("Private connected peer was permitted")
        sock.close.assert_called_once()
        adapter.close()
        evidence["private_connected_peer_blocked"] = True
        evidence["automatic_retries_disabled"] = True
        evidence["live_catalog_writes"] = 0
        return evidence
    finally:
        frappe.set_user(actor)


def _default_snapshot(row):
    with source_api_context("https://dev-priv-api.kaleam.net"):
        return snapshot(row)
