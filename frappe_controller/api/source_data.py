"""Authenticated Desk facade for the external reference-data catalog."""

from datetime import UTC, datetime
from contextlib import contextmanager
from functools import wraps
from inspect import signature
import hashlib
import json
import re
from uuid import UUID, uuid4

import frappe

from ..compat import CONTROLLER_ROLES, SOURCE_DATA_ROLE
from ..feature_flags import runtime_feature_config
from ..source_data import (
    API_ORIGIN, CATALOGS, MAX_ADD_ROWS, RECORD_RESOURCES, SourceDataError,
    _integer, add_record, add_vehicle, delete_record, edit_record, fetch_records, get_record,
    prepare_request, require_catalog, result_page, snapshot, validate_add_records, validate_edit_changes,
    get_api_origin, normalize_api_url, source_api_context,
)


READ_ROLES = CONTROLLER_ROLES | {SOURCE_DATA_ROLE}
WRITE_ROLES = frozenset({"System Manager", "Controller Admin", SOURCE_DATA_ROLE})


def _actor(roles):
    user = frappe.session.user
    if not user or user == "Guest" or not roles.intersection(frappe.get_roles(user)):
        frappe.throw("A permitted Controller role is required for this action.", frappe.PermissionError)
    if not runtime_feature_config(frappe).workspace_visible:
        frappe.throw("Frappe Controller is disabled.", frappe.PermissionError)
    return user


def _json(value):
    if isinstance(value, str):
        if len(value) > 64 * 1024:
            raise SourceDataError("Source-data request is too large.")
        try:
            return json.loads(value)
        except ValueError:
            raise SourceDataError("Supply valid JSON.") from None
    return value


def _guarded_adapter_factory():
    # Reuse Frappe v16's DNS and connected-peer checks, with retries disabled
    # because a catalog mutation must never be repeated automatically.
    from frappe.utils.safe_exec import BlockedRequest, _get_ssrf_guarded_adapter_class

    class SourceAPIAdapter(_get_ssrf_guarded_adapter_class()):
        def send(self, *args, **kwargs):
            try:
                return super().send(*args, **kwargs)
            except BlockedRequest:
                raise SourceDataError("Source Data API URL must resolve to a reachable public host. Check Controller Settings.") from None

    return SourceAPIAdapter


def _source_api(roles):
    def decorate(fn):
        parameters = signature(fn)

        @wraps(fn)
        def wrapped(*args, **kwargs):
            _actor(roles)  # Authorization precedes settings, cache and network.
            try:
                origin = normalize_api_url(frappe.db.get_single_value(
                    "Frappe Controller Settings", "source_data_api_url", cache=False) or API_ORIGIN)
                reviewed_source = parameters.bind(*args, **kwargs).arguments.get("source")
                if reviewed_source is not None and normalize_api_url(reviewed_source) != origin:
                    raise SourceDataError("Source Data API URL changed. Reload Source Data and reopen the form before saving.")
                with source_api_context(origin, _guarded_adapter_factory()):
                    return fn(*args, **kwargs)
            except SourceDataError as error:
                frappe.throw(str(error), frappe.ValidationError)

        return wrapped
    return decorate


@frappe.whitelist(methods=["GET"])
@_source_api(READ_ROLES)
def browse(resource="brands", filters=None, start=0, page_length=50, source=None):
    user = frappe.session.user
    try:
        if isinstance(filters, str):
            try:
                filters = json.loads(filters)
            except ValueError:
                raise SourceDataError("Filters must be valid JSON.") from None
        request = prepare_request(resource, {} if filters is None else filters, start, page_length)
    except SourceDataError as error:
        frappe.throw(str(error), frappe.ValidationError)
    # Authorization always precedes cache access. Only successful, validated
    # responses are cached, site-locally, for one minute.
    generation = frappe.cache.get_value("controller-source-data-generation") or "0"
    identity = json.dumps([get_api_origin(), resource, request["path"], request["params"], generation], sort_keys=True)
    key = "controller-source-data:" + hashlib.sha256(identity.encode()).hexdigest()
    cached = frappe.cache.get_value(key)
    if cached is None:
        try:
            records = fetch_records(request)
        except SourceDataError as error:
            frappe.throw(str(error), frappe.ValidationError)
        cached = {"records": records, "total": request.get("total"), "fetched_at": datetime.now(UTC).isoformat()}
        frappe.cache.set_value(key, cached, expires_in_sec=60)
    request["total"] = cached.get("total")
    result = result_page(request, cached["records"], cached["fetched_at"])
    result["can_write"] = bool(WRITE_ROLES.intersection(frappe.get_roles(user)))
    result["can_manage_settings"] = bool(frappe.has_permission("Frappe Controller Settings", ptype="write"))
    result["can_add_vehicles"] = result["can_write"]
    result["catalogs"] = CATALOGS
    result["snapshots"] = {str(row["id"]): snapshot(row) for row in result["items"] if type(row.get("id")) is int and row["id"] > 0} if resource in RECORD_RESOURCES else {}
    result["max_add_rows"] = MAX_ADD_ROWS
    return result


@frappe.whitelist(methods=["POST"])
@_source_api(WRITE_ROLES)
def validate_add(records, resource="vehicles", source=None):
    try:
        return {"records": validate_add_records(_json(records), resource), "max_add_rows": MAX_ADD_ROWS}
    except SourceDataError as error:
        frappe.throw(str(error), frappe.ValidationError)


@contextmanager
def _write_lock(key):
    lock = frappe.cache.lock(key, timeout=120)
    if not lock.acquire(blocking=False):
        frappe.throw("A matching change is in progress. Check its result before trying again.", frappe.ValidationError)
    try:
        yield
    finally:
        lock.release()


@frappe.whitelist(methods=["POST"])
@_source_api(WRITE_ROLES)
def add(record, request_id, resource="vehicles", source=None):
    """One record per POST; bulk callers validate the full list before starting.

    Persist intent before contacting the API. Repeating a request ID returns its
    recorded outcome, including uncertainty, rather than repeating the write.
    Integration Request logs follow Frappe's standard log retention policy.
    """
    user = frappe.session.user
    try:
        record = validate_add_records([_json(record)], resource)[0]
    except SourceDataError as error:
        frappe.throw(str(error), frappe.ValidationError)
    return _change("add", resource, record, request_id, user,
                   lambda: add_vehicle(record) if resource == "vehicles" else add_record(resource, record))


def _delete_targets(resource, records):
    require_catalog(resource)
    records = _json(records)
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_ADD_ROWS:
        raise SourceDataError("Select between 1 and 100 records.")
    seen, result = set(), []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"id", "snapshot"}:
            raise SourceDataError("Deletion requires an explicit ID and reviewed snapshot for every record.")
        identity = _integer(record["id"], "record ID", 1, 2147483647)
        if identity in seen or not isinstance(record["snapshot"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["snapshot"]):
            raise SourceDataError("Invalid or duplicate deletion target. Reload the records.")
        seen.add(identity)
        result.append({"id": identity, "snapshot": record["snapshot"]})
    return result


@frappe.whitelist(methods=["POST"])
@_source_api(WRITE_ROLES)
def validate_delete(resource, records, source=None):
    try:
        return {"records": _delete_targets(resource, records)}
    except SourceDataError as error:
        frappe.throw(str(error), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
@_source_api(WRITE_ROLES)
def remove(resource, record_id, snapshot_hash, request_id, source=None):
    user = frappe.session.user
    try:
        record = _delete_targets(resource, [{"id": record_id, "snapshot": snapshot_hash}])[0]
    except SourceDataError as error:
        frappe.throw(str(error), frappe.ValidationError)
    return _change("delete", resource, record, request_id, user,
                   lambda: delete_record(resource, record["id"], record["snapshot"]))


@frappe.whitelist(methods=["GET"])
@_source_api(READ_ROLES)
def read(resource, record_id, source=None):
    try:
        record = get_record(resource, record_id)
        return {"record": record, "snapshot": snapshot(record), "source": get_api_origin()}
    except SourceDataError as error:
        frappe.throw(str(error), frappe.ValidationError)


def _edit_payload(resource, record_id, changes, snapshot_hash):
    require_catalog(resource)
    identity = _integer(record_id, "record ID", 1, 2147483647)
    if not isinstance(snapshot_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash):
        raise SourceDataError("Invalid reviewed record. Reopen the editor.")
    return {"id": identity, "snapshot": snapshot_hash, "changes": validate_edit_changes(resource, _json(changes))}


@frappe.whitelist(methods=["POST"])
@_source_api(WRITE_ROLES)
def validate_edit(resource, record_id, changes, snapshot_hash, source=None):
    try:
        return _edit_payload(resource, record_id, changes, snapshot_hash)
    except SourceDataError as error:
        frappe.throw(str(error), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
@_source_api(WRITE_ROLES)
def edit(resource, record_id, changes, snapshot_hash, request_id, source=None):
    user = frappe.session.user
    try:
        record = _edit_payload(resource, record_id, changes, snapshot_hash)
    except SourceDataError as error:
        frappe.throw(str(error), frappe.ValidationError)
    return _change("edit", resource, record, request_id, user,
                   lambda: edit_record(resource, record["id"], record["changes"], record["snapshot"]))


def _change(action, resource, record, request_id, user, perform):
    try:
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, AttributeError):
        frappe.throw("Invalid change request ID.", frappe.ValidationError)
    origin = get_api_origin()
    payload = json.dumps({"user": user, "origin": origin, "action": action, "resource": resource, "record": record}, sort_keys=True, ensure_ascii=False)
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    lock_record = {"id": record["id"]} if action in {"edit", "delete"} else {key: value.casefold() if isinstance(value, str) else value for key, value in record.items()}
    record_hash = hashlib.sha256(json.dumps([origin, resource, lock_record], sort_keys=True).encode()).hexdigest()
    name = "controller-source-" + action + "-" + UUID(request_id).hex
    with _write_lock("controller-source-request:" + request_id):
        for other_action in {"add", "edit", "delete"} - {action}:
            other = "controller-source-" + other_action + "-" + UUID(request_id).hex
            if frappe.db.exists("Integration Request", other):
                frappe.throw("This change request ID belongs to a different action.", frappe.ValidationError)
        if frappe.db.exists("Integration Request", name):
            log = frappe.get_doc("Integration Request", name)
            if log.data != payload:
                frappe.throw("This change request ID belongs to a different request.", frappe.ValidationError)
            return json.loads(log.output) if log.output else {
                "state": "uncertain", "message": "This change is in progress or was interrupted. Check the source before trying again.",
                "request_id": request_id, **record,
            }
        # Serialize matching rows even when they have different request IDs.
        with _write_lock("controller-source-record:" + record_hash):
            log = frappe.get_doc({
                "doctype": "Integration Request", "integration_request_service": "Controller Source Data",
                "request_id": request_id, "status": "Queued", "data": payload,
                "url": origin + "/tires/" + CATALOGS[resource]["singular"] + ("/create" if action == "add" else "/" + action + "/" + str(record["id"])),
                "request_description": action.title() + " " + resource + " " + payload_hash[:16],
            })
            log.flags._name = name
            log.insert(ignore_permissions=True)
            frappe.db.commit()
            try:
                result = perform()
            except SourceDataError as error:
                result = {"state": "failed", "message": str(error) + " No change was submitted."}
            result.update({"request_id": request_id, **record})
            log.status = "Completed" if result["state"] in {"added", "existing", "deleted", "missing", "updated", "unchanged"} else "Failed" if result["state"] == "failed" else "Queued"
            log.output = json.dumps(result, ensure_ascii=False)
            log.save(ignore_permissions=True)
            frappe.db.commit()
            if result["state"] != "failed":
                frappe.cache.set_value("controller-source-data-generation", uuid4().hex)
            return result
