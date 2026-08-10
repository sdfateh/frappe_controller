"""Execution-free controller contract for data-update preview and promotion."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, Mapping

from .operation_service import OperationAuthoringError, PayloadAuthorization


MAXIMUM_ROWS = 100
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,139}$")
_DOCTYPE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,139}$")
_POLICY = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_OPERATIONS = frozenset({"data.update", "data.update.break_glass"})
_OPERATORS = frozenset({"eq", "ne", "in", "not_in", "lt", "lte", "gt", "gte"})
_SYSTEM = frozenset({
    "_assign", "_comments", "_liked_by", "_seen", "_user_tags", "creation",
    "docstatus", "doctype", "idx", "modified", "modified_by", "name", "owner",
    "parent", "parentfield", "parenttype", "allow_on_submit", "autoname",
    "depends_on", "fieldname", "fieldtype", "hidden", "options", "permlevel",
    "read_only", "reqd", "unique", "roles", "username", "user_type",
})
_FORBIDDEN_DOCTYPES = frozenset({
    "agent certificate", "authentication log", "client script", "custom docperm",
    "custom field", "connected app", "docfield", "docperm", "doctype", "has role",
    "installed applications", "module def", "oauth bearer token", "oauth client",
    "oauth scope", "operation approval", "property setter", "role", "role profile",
    "server script", "singles", "system settings", "user permission", "user",
})
_SECRET_WORDS = frozenset({
    "auth", "authorization", "credential", "credentials", "password", "passwd",
    "secret", "token", "api_key", "api_secret", "private_key", "encryption_key",
})


def _fail(message: str = "data update payload is invalid") -> None:
    raise OperationAuthoringError(message)


def _words(value: str) -> set[str]:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return {item for item in re.sub(r"[^a-z0-9]+", "_", snake.casefold()).split("_") if item}


def _secret(value: str) -> bool:
    words = _words(value)
    normalized = re.sub(r"[^a-z0-9]+", "_", re.sub(
        r"([a-z0-9])([A-Z])", r"\1_\2", value
    ).casefold()).strip("_")
    return bool(words & {
        "auth", "authorization", "credential", "credentials", "password",
        "passwd", "secret", "token",
    }) or normalized in _SECRET_WORDS


def _field(value: Any) -> str:
    if not isinstance(value, str) or value != unicodedata.normalize("NFC", value) or not _FIELD.fullmatch(value):
        _fail()
    if value.casefold() in _SYSTEM or _secret(value):
        _fail("sensitive, authentication, schema, and system fields are forbidden")
    return value


def _doctype(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or value != unicodedata.normalize("NFC", value) or not _DOCTYPE.fullmatch(value):
        _fail()
    normalized = value.casefold()
    if normalized in _FORBIDDEN_DOCTYPES or normalized.startswith(("oauth ", "integration request")) or _secret(value):
        _fail("authentication, schema, and system DocTypes are forbidden")
    return value


def _text(value: Any, maximum: int) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or value != unicodedata.normalize("NFC", value) or len(value.encode()) > maximum
        or "\x00" in value or "\r" in value or "\n" in value
    ):
        _fail()
    return value


def _value(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 8 or budget[0] > 1000:
        _fail()
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > 10**38 - 1:
            _fail()
        return value
    if type(value) is float:
        if not math.isfinite(value) or abs(value) > 1e100:
            _fail()
        return value
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        if "\x00" in normalized or len(normalized.encode()) > 32 * 1024:
            _fail()
        return normalized
    if isinstance(value, list):
        if len(value) > 100:
            _fail()
        return [_value(item, depth=depth + 1, budget=budget) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 32:
            _fail()
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key or key != key.strip() or len(key.encode()) > 140 or _secret(key):
                _fail("secret-shaped nested JSON fields are forbidden")
            normalized = unicodedata.normalize("NFC", key)
            if normalized != key or normalized in result:
                _fail()
            result[normalized] = _value(child, depth=depth + 1, budget=budget)
        return result
    _fail()


def _timestamp(value: Any) -> str:
    value = _text(value, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail()
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_data_update_payload(raw: Mapping[str, Any], *, dry_run: bool) -> dict[str, Any]:
    fields = {
        "contract_version", "doctype", "document_names", "filters", "changes",
        "expected_modified", "dry_run", "maximum_rows", "reason",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields or raw.get("contract_version") != "1.0" or raw.get("dry_run") is not dry_run:
        _fail()
    maximum_rows = raw.get("maximum_rows")
    if type(maximum_rows) is not int or not 1 <= maximum_rows <= MAXIMUM_ROWS:
        _fail()
    names = raw.get("document_names")
    filters = raw.get("filters")
    has_names = isinstance(names, list) and bool(names)
    has_filters = isinstance(filters, list) and bool(filters)
    if has_names == has_filters:
        _fail()
    normalized_names = None
    if has_names:
        if len(names) > 100:
            _fail()
        normalized_names = [_text(name, 140) for name in names]
        if len({name.casefold() for name in normalized_names}) != len(normalized_names):
            _fail()
    elif names is not None:
        _fail()

    normalized_filters = None
    if has_filters:
        if len(filters) > 20:
            _fail()
        normalized_filters = []
        for item in filters:
            if not isinstance(item, Mapping) or set(item) != {"field", "operator", "value"}:
                _fail()
            operator = item["operator"]
            if operator not in _OPERATORS:
                _fail()
            filter_value = _value(item["value"])
            if operator in {"in", "not_in"}:
                if not isinstance(filter_value, list) or not filter_value or any(isinstance(child, (list, Mapping)) for child in filter_value):
                    _fail()
            elif isinstance(filter_value, (list, Mapping)):
                _fail()
            if operator in {"lt", "lte", "gt", "gte"} and filter_value is None:
                _fail()
            normalized_filters.append({"field": _field(item["field"]), "operator": operator, "value": filter_value})
    elif filters is not None:
        _fail()

    changes = raw.get("changes")
    if not isinstance(changes, Mapping) or not changes or len(changes) > 32:
        _fail()
    normalized_changes = {_field(key): _value(value) for key, value in changes.items()}
    expected = raw.get("expected_modified")
    normalized_expected = None if expected is None else _timestamp(expected)
    if normalized_expected is not None and (maximum_rows != 1 or not normalized_names or len(normalized_names) != 1):
        _fail()
    result = {
        "contract_version": "1.0",
        "doctype": _doctype(raw["doctype"]),
        "document_names": normalized_names,
        "filters": normalized_filters,
        "changes": normalized_changes,
        "expected_modified": normalized_expected,
        "dry_run": dry_run,
        "maximum_rows": maximum_rows,
        "reason": _text(raw["reason"], 500),
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > 128 * 1024:
        _fail()
    return result


def normalize_data_update_command(
    operation: str, raw: Mapping[str, Any], *, dry_run: bool
) -> dict[str, Any]:
    if operation not in _OPERATIONS or not isinstance(raw, Mapping) or set(raw) != {"policy_id", "payload"}:
        _fail("unsupported data update operation")
    policy_id = raw.get("policy_id")
    if not isinstance(policy_id, str) or not _POLICY.fullmatch(policy_id):
        _fail("invalid local policy id")
    return {
        "policy_id": policy_id,
        "payload": normalize_data_update_payload(raw["payload"], dry_run=dry_run),
    }


def data_update_authorization(
    operation: str, raw: Mapping[str, Any], *, preview: bool
) -> PayloadAuthorization:
    return PayloadAuthorization(
        normalized_payload=normalize_data_update_command(operation, raw, dry_run=preview),
        site_required=True,
        destructive=False,
        pre_operation_backup=False,
        approval_required=not preview,
        payload_contains_site_identity=False,
        pre_approval_preview=preview,
    )


def canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "canonical_hash", "data_update_authorization", "normalize_data_update_command",
    "normalize_data_update_payload",
]
