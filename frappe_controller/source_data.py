"""Bounded client for the documented Kaleam source-data API.

The origin comes from administrator settings; routes and methods are fixed.
No credentials or upstream response bodies are included in error messages.
"""

from __future__ import annotations

import json
import hashlib
import math
import ipaddress
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import time
from urllib.parse import quote, urlsplit

import requests


API_ORIGIN = "https://dev-priv-api.kaleam.net"
_api_context = ContextVar("controller_source_api", default=None)
CATALOGS = json.loads(Path(__file__).with_name("source_data_schema.json").read_text())
RECORD_RESOURCES = {"vehicle_records": "vehicles", "tires": "tires", "colors": "colors"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 10000
MAX_ADD_ROWS = 100
LOOKUPS = {
    "brands": "brand",
    "models": "model",
    "years": "year",
    "structures": "structureType",
    "engines": "engine",
}


class SourceDataError(ValueError):
    """A safe, user-facing validation or upstream error."""


def normalize_api_url(value):
    """Only a public HTTPS origin; credentials and API paths are not settings."""
    message = "Source Data API URL must be a plain public HTTPS origin without credentials, path, query or fragment."
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value.strip()):
        raise SourceDataError(message)
    value = value.strip()
    if not value:
        return API_ORIGIN
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or "?" in value or "#" in value):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or "." not in host or host.endswith((".localhost", ".local", ".internal"))
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split("."))):
                raise ValueError
        else:
            if not address.is_global or address.is_multicast:
                raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SourceDataError(message) from None
    host = f"[{host}]" if ":" in host else host
    return "https://" + host + (f":{port}" if port not in {None, 443} else "")


def get_api_origin():
    context = _api_context.get()
    return context[0] if context else API_ORIGIN


@contextmanager
def source_api_context(origin, adapter_factory=None):
    """Keep one settings value across preflight, journal commits and writes."""
    token = _api_context.set((normalize_api_url(origin), adapter_factory))
    try:
        yield
    finally:
        _api_context.reset(token)


def _session():
    session = requests.Session()
    session.trust_env = False
    context = _api_context.get()
    if context and context[1]:
        session.mount("https://", context[1](max_retries=0))
    return session


def _text(value: object, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise SourceDataError(f"Invalid {label}.")
    value = value.strip()
    if (required and not value) or len(value) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SourceDataError(f"Invalid {label}.")
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SourceDataError(f"Invalid {label}.")
    try:
        number = int(value)
    except ValueError:
        raise SourceDataError(f"Invalid {label}.") from None
    if not minimum <= number <= maximum:
        raise SourceDataError(f"Invalid {label}.")
    return number


def prepare_request(resource: str, filters: object, start: object, page_length: object) -> dict:
    if not isinstance(resource, str) or resource not in {*LOOKUPS, *RECORD_RESOURCES}:
        raise SourceDataError("Unknown source-data resource.")
    if not isinstance(filters, dict):
        raise SourceDataError("Filters must be an object.")
    offset = _integer(start, "start", 0, 2147483647)
    length = _integer(page_length, "page length", 1, 100)
    if resource in RECORD_RESOURCES:
        catalog = RECORD_RESOURCES[resource]
        values = validate_fields(catalog, filters, filtering=True)
        return {"resource": resource, "path": f"/tires/{CATALOGS[catalog]['singular']}/get/list",
                "params": {"start": offset, "limit": length + 1, "filters": json.dumps(values, ensure_ascii=False)},
                "start": offset, "page_length": length}
    required = {
        "brands": (), "models": ("brand",), "years": ("brand", "model"),
        "structures": ("brand", "model", "year"),
        "engines": ("brand", "model", "year", "structure_type"),
        "tires": (), "colors": (),
    }[resource]
    if set(filters) != set(required):
        raise SourceDataError("Supply only the required filters for this resource.")
    values = {key: _text(value, key) for key, value in filters.items()}
    if "year" in values:
        values["year"] = str(_integer(values["year"], "year", 1886, 2200))
    # Encoded slashes may be decoded by proxies, and dot segments normalized by
    # HTTP clients. Keep these valid catalog names entirely out of URL paths.
    if resource in {"models", "years"} and any(
        value in {".", ".."} or any(char in value for char in "/\\%")
        for value in values.values()
    ):
        return {"resource": resource, "path": "/tires/vehicle/get/list",
                "params": {"filters": json.dumps(values, ensure_ascii=False), "start": 0, "limit": 100},
                "project_lookup": True, "start": offset, "page_length": length}
    segment = lambda key: quote(values[key], safe="")
    paths = {
        "brands": "/tires/vehicle/brand/list",
        "tires": "/tires/tire", "colors": "/tires/color",
        "structures": "/tires/vehicle/getStructureType",
        "engines": "/tires/vehicle/getEngineCapacity",
    }
    if resource == "models":
        path = f"/tires/vehicle/model/{segment('brand')}"
    elif resource == "years":
        path = f"/tires/vehicle/year/{segment('brand')}/{segment('model')}"
    else:
        path = paths[resource]
    params = {}
    if resource in {"structures", "engines"}:
        params = {key: values[key] for key in ("brand", "model", "year")}
        if resource == "engines":
            params["structureType"] = values["structure_type"]
    elif resource == "colors":
        params = {"start": offset, "limit": length + 1}
    # The Tire GET documents no pagination. Fetch a bounded response and slice
    # it locally instead of claiming that undocumented parameters are honored.
    return {"resource": resource, "path": path, "params": params, "start": offset, "page_length": length}


def _get_json(path: str, params=None, *, missing_ok=False):
    deadline = time.monotonic() + 20
    try:
        with _session() as session:
            with session.get(
                get_api_origin() + path, params=params or {},
                headers={"Accept": "application/json"}, timeout=(3.05, 15),
                allow_redirects=False, stream=True,
            ) as response:
                if response.status_code == 404 and missing_ok:
                    return None, None
                if response.status_code != 200:
                    raise SourceDataError(f"Source API is unavailable (HTTP {response.status_code}). Please try again later.")
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise SourceDataError("Source API returned an unexpected response format.")
                body = bytearray()
                total = response.headers.get("X-Total-Count")
                if total is not None:
                    try:
                        total = _integer(total, "source total count", 0, 2147483647)
                    except SourceDataError:
                        raise SourceDataError("Source API returned an invalid total count.") from None
                for chunk in response.iter_content(64 * 1024):
                    if time.monotonic() > deadline:
                        raise SourceDataError("Source API response timed out.")
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise SourceDataError("Source API response exceeds the browsing limit.")
    except requests.RequestException:
        raise SourceDataError("Cannot connect to the source API. Please try again later.") from None
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        raise SourceDataError("Source API returned invalid JSON.") from None
    return value, total


def fetch_records(request: dict) -> list[dict]:
    if request.get("project_lookup"):
        return _fetch_filtered_lookup(request)
    records, total = _get_json(request["path"], request["params"])
    request["total"] = total
    if not isinstance(records, list) or len(records) > MAX_RECORDS or any(not isinstance(row, dict) for row in records):
        raise SourceDataError("Source API returned an unexpected record structure.")
    field = LOOKUPS.get(request["resource"])
    if field:
        if any(field not in row or (row[field] is not None and (not isinstance(row[field], (str, int)) or isinstance(row[field], bool))) for row in records):
            raise SourceDataError("Source API returned an unexpected lookup structure.")
        # A newly added brand/model has no year, body style or engine yet.
        records = [row for row in records if row[field] is not None and str(row[field]).strip()]
    return records


def _fetch_filtered_lookup(request: dict) -> list[dict]:
    """Read a bounded exact-match catalog and return distinct selector values."""
    field = LOOKUPS[request["resource"]]
    values = {}
    offset, size, byte_count = 0, 100, 0
    deadline = time.monotonic() + 20
    while offset < MAX_RECORDS:
        if time.monotonic() > deadline:
            raise SourceDataError("Source API response timed out.")
        rows, total = _get_json(request["path"], {**request["params"], "start": offset})
        if time.monotonic() > deadline:
            raise SourceDataError("Source API response timed out.")
        if not isinstance(rows, list) or len(rows) > size or any(not isinstance(row, dict) for row in rows):
            raise SourceDataError("Source API returned an unexpected record structure.")
        byte_count += len(json.dumps(rows, ensure_ascii=False).encode())
        if byte_count > MAX_RESPONSE_BYTES or (total is not None and total > MAX_RECORDS):
            raise SourceDataError("Source API response exceeds the browsing limit.")
        for row in rows:
            value = row.get(field)
            if field not in row or (value is not None and (not isinstance(value, (str, int)) or isinstance(value, bool))):
                raise SourceDataError("Source API returned an unexpected lookup structure.")
            if value is not None and str(value).strip():
                values.setdefault(str(value), {field: value})
        offset += len(rows)
        if total is not None:
            if offset > total or (len(rows) < size and offset < total):
                raise SourceDataError("Source API returned an inconsistent total count.")
            complete = offset == total
        else:
            complete = len(rows) < size
        if complete:
            request["total"] = len(values)
            return list(values.values())
    raise SourceDataError("Source API response exceeds the browsing limit.")


def snapshot(record: dict) -> str:
    return hashlib.sha256(json.dumps([get_api_origin(), record], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def require_catalog(resource):
    if not isinstance(resource, str) or resource not in CATALOGS:
        raise SourceDataError("Unknown source catalog.")
    return CATALOGS[resource]


def validate_fields(resource, record, *, filtering=False, partial=False):
    schema = require_catalog(resource)
    fields = schema["fields"]
    if filtering:
        fields = {**fields, "id": {"type": "integer", "minimum": 1, "maximum": 2147483647}}
    if not isinstance(record, dict) or set(record) - set(fields):
        raise SourceDataError("Supply only supported catalog fields.")
    if not filtering and not partial and any(key not in record for key in schema["required"]):
        raise SourceDataError("Missing required catalog fields: " + ", ".join(schema["required"]) + ".")
    result = {}
    for key, value in record.items():
        field = fields[key]
        required = key in schema["required"] and not filtering
        if value is None:
            if field.get("nullable"):
                result[key] = None
                continue
            raise SourceDataError(f"{key} cannot be null.")
        if field["type"] in {"integer", "number"}:
            if key in {"year", "id"}:
                result[key] = _integer(value, key, 1886 if key == "year" else 1, 2200 if key == "year" else 2147483647)
            else:
                try:
                    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                        raise ValueError
                    number = float(value)
                    if not math.isfinite(number) or not field["minimum"] <= number <= field["maximum"]:
                        raise ValueError
                except ValueError:
                    raise SourceDataError(f"Invalid {key}.") from None
                result[key] = number
        else:
            if not isinstance(value, str):
                raise SourceDataError(f"Invalid {key}.")
            value = value.strip()
            if (required and not value) or len(value) > field["maxLength"] or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise SourceDataError(f"Invalid {key}.")
            result[key] = value
    if resource == "vehicles" and "brand" in result and "model" in result:
        display = f"{result['brand']} {result['model']}" + (f" {result['year']}" if result.get("year") else "")
        if len(display) > 150:
            raise SourceDataError("The combined vehicle name must fit 150 characters.")
    return result


def validate_edit_changes(resource, changes):
    result = validate_fields(resource, changes, partial=True)
    if not result:
        raise SourceDataError("Supply at least one changed field.")
    return result


def get_record(resource, record_id):
    schema = require_catalog(resource)
    record_id = _integer(record_id, "record ID", 1, 2147483647)
    current, _ = _get_json(f"/tires/{schema['singular']}/get/{record_id}", missing_ok=True)
    if current is None:
        raise SourceDataError("This record no longer exists. Reload the records.")
    if not isinstance(current, dict) or type(current.get("id")) is not int or current["id"] != record_id:
        raise SourceDataError("Source API returned an unexpected record structure.")
    return current


def edit_record(resource, record_id, changes, expected_snapshot):
    schema = require_catalog(resource)
    changes = validate_edit_changes(resource, changes)
    current = get_record(resource, record_id)
    if snapshot(current) != expected_snapshot:
        return {"state": "failed", "message": "Record changed since review. Reopen the editor and review it again."}
    if resource == "vehicles" and {"brand", "model", "year"}.intersection(changes):
        # A one-field patch must still respect the combined display-name limit.
        merged = {**current, **changes}
        validate_fields(resource, {key: merged[key] for key in ("brand", "model", "year") if key in merged}, partial=True)
    def same(key, value):
        old = current.get(key)
        if schema["fields"][key]["type"] == "number" and old is not None and value is not None:
            try:
                return float(old) == float(value)
            except (ValueError, TypeError):
                return False
        return old == value
    if all(same(key, value) for key, value in changes.items()):
        return {"state": "unchanged", "message": "No changes detected. No update submitted."}
    return _write("PUT", f"/tires/{schema['singular']}/edit/{current['id']}", changes)


def validate_add_records(records: object, resource="vehicles") -> list[dict]:
    require_catalog(resource)
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_ADD_ROWS:
        raise SourceDataError(f"Supply between 1 and {MAX_ADD_ROWS} rows.")
    normalized, seen = [], set()
    for index, row in enumerate(records, 1):
        try:
            value = validate_fields(resource, row)
        except SourceDataError as error:
            raise SourceDataError(f"Row {index}: {error}") from None
        identity = json.dumps({key: text.casefold() if isinstance(text, str) else text for key, text in value.items()}, sort_keys=True)
        if identity in seen:
            raise SourceDataError(f"Row {index}: duplicate record in this batch.")
        seen.add(identity)
        normalized.append(value)
    return normalized


def add_vehicle(record: dict[str, str]) -> dict[str, str]:
    return add_record("vehicles", record)


def add_record(resource, record):
    schema = require_catalog(resource)
    request = prepare_request("vehicle_records" if resource == "vehicles" else resource, record, 0, 1)
    if fetch_records(request):
        return {"state": "existing", "message": "A matching record already exists. No change made."}
    return _write("POST", f"/tires/{schema['singular']}/create", record)


def delete_record(resource, record_id, expected_snapshot):
    schema = require_catalog(resource)
    record_id = _integer(record_id, "record ID", 1, 2147483647)
    current, _ = _get_json(f"/tires/{schema['singular']}/get/{record_id}", missing_ok=True)
    if current is None:
        return {"state": "missing", "message": "Record is already absent. No deletion submitted."}
    if not isinstance(current, dict) or current.get("id") != record_id or snapshot(current) != expected_snapshot:
        return {"state": "failed", "message": "Record changed since review. Reload and review it again."}
    return _write("DELETE", f"/tires/{schema['singular']}/delete/{record_id}")


def _write(method, path, record=None):
    """A single attempt: unacknowledged mutations are never retried."""
    try:
        with _session() as session:
            sender = {"POST": session.post, "PUT": session.put, "DELETE": session.delete}[method]
            kwargs = {"json": record} if method in {"POST", "PUT"} else {}
            with sender(get_api_origin() + path, headers={"Accept": "application/json"},
                        timeout=(3.05, 15), allow_redirects=False, stream=True, **kwargs) as response:
                status = response.status_code
                if status in ({201} if method == "POST" else {200, 204}):
                    state, message = {"POST": ("added", "Record added."), "PUT": ("updated", "Record updated."), "DELETE": ("deleted", "Record deleted.")}[method]
                    return {"state": state, "message": message}
                if status == 409 and method == "DELETE":
                    return {"state": "failed", "message": "Deletion blocked: this record has linked records. No force deletion was attempted."}
                if status in {400, 401, 403, 404, 405, 409, 413, 415, 422, 429}:
                    return {"state": "failed", "message": f"Source API rejected the change (HTTP {status})."}
                return {"state": "uncertain", "message": f"Source API did not confirm the change (HTTP {status}). Check the source before trying again."}
    except requests.RequestException:
        return {"state": "uncertain", "message": "The change was not acknowledged. Check the source before trying again."}


def result_page(request: dict, records: list[dict], fetched_at: str) -> dict:
    resource, start, length = (request[key] for key in ("resource", "start", "page_length"))
    if resource in LOOKUPS:
        # Dependent selectors need the entire bounded lookup, not one page.
        items, total, more = records, len(records), False
    else:
        items, total = records[:length], request.get("total")
        more = start + len(items) < total if total is not None else len(records) > length
    return {"resource": resource, "items": items, "total": total, "has_more": more,
            "start": start, "page_length": length, "fetched_at": fetched_at, "source": get_api_origin()}
