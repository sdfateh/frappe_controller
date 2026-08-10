"""Dependency-free controller normalization for the Phase 4 lifecycle contract."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .operation_service import OperationAuthoringError, PayloadAuthorization


_DOMAIN = re.compile(
    r"^(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE = ("api_key", "credential", "encryption_key", "password", "private_key", "secret", "token")

_CREATE_BACKUP = frozenset({"site.create", "site.create_from_backup"})
_BASE_ONLY = frozenset({
    "site.create_blank", "site.migrate", "site.scheduler.enable",
    "site.scheduler.disable", "site.maintenance.enable", "site.maintenance.disable",
})
_DESTRUCTIVE = frozenset({"site.restore", "site.reinstall", "site.delete"})
LIFECYCLE_AUTHORING_OPERATIONS = frozenset(
    {*_CREATE_BACKUP, *_BASE_ONLY, *_DESTRUCTIVE, "site.backup", "site.config.update"}
)


def _fail() -> None:
    raise OperationAuthoringError("lifecycle payload is invalid")


def _domain(value: Any) -> str:
    if not isinstance(value, str):
        _fail()
    normalized = value.rstrip(".").lower()
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        _fail()
    if not _DOMAIN.fullmatch(normalized):
        _fail()
    return normalized


def _base(payload: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    if set(payload) - allowed or "domain" not in payload:
        _fail()
    timeout = payload.get("execution_timeout_seconds", 3600)
    if type(timeout) is not int or not 60 <= timeout <= 86_400:
        _fail()
    digest = payload.get("expected_inventory_digest")
    if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
        _fail()
    return {
        "domain": _domain(payload["domain"]),
        "execution_timeout_seconds": timeout,
        "expected_inventory_digest": digest,
    }


def _trimmed_text(value: Any, minimum: int, maximum: int) -> str:
    if (
        not isinstance(value, str) or not minimum <= len(value) <= maximum
        or value != value.strip() or "\x00" in value
    ):
        _fail()
    return value


def validate_lifecycle_payload(
    operation: str, raw_payload: Mapping[str, Any]
) -> PayloadAuthorization:
    """Normalize exactly the same data-only fields accepted by the agent."""
    if operation not in LIFECYCLE_AUTHORING_OPERATIONS or not isinstance(raw_payload, Mapping):
        raise OperationAuthoringError("unsupported lifecycle operation")
    payload = dict(raw_payload)
    common = {"domain", "execution_timeout_seconds", "expected_inventory_digest"}

    if operation in _CREATE_BACKUP or operation in {"site.restore", "site.reinstall"}:
        extra = {
            "template_name", "bucket_name", "backup_prefix", "restore_public_files",
            "restore_private_files", "restore_encryption_key",
        }
        if operation in _DESTRUCTIVE:
            extra |= {"confirmation", "pre_operation_backup"}
        normalized = _base(payload, common | extra)
        normalized["template_name"] = _trimmed_text(payload.get("template_name"), 1, 253)
        bucket = payload.get("bucket_name")
        prefix = payload.get("backup_prefix")
        if bucket is not None:
            bucket = _trimmed_text(bucket, 3, 63)
        if prefix is not None:
            prefix = _trimmed_text(prefix, 0, 512)
        normalized.update(bucket_name=bucket, backup_prefix=prefix)
        for field in ("restore_public_files", "restore_private_files", "restore_encryption_key"):
            value = payload.get(field, False)
            if type(value) is not bool:
                _fail()
            normalized[field] = value
    elif operation == "site.backup":
        normalized = _base(payload, common | {"with_files"})
        with_files = payload.get("with_files", True)
        if type(with_files) is not bool:
            _fail()
        normalized["with_files"] = with_files
    elif operation == "site.delete":
        normalized = _base(payload, common | {"confirmation", "pre_operation_backup", "quarantine_days", "purge"})
        days = payload.get("quarantine_days", 7)
        if type(days) is not int or not 1 <= days <= 90 or payload.get("purge", False) is not False:
            _fail()
        normalized.update(quarantine_days=days, purge=False)
    elif operation == "site.config.update":
        normalized = _base(payload, common | {"key", "value", "reason"})
        key = payload.get("key")
        if not isinstance(key, str) or not _CONFIG_KEY.fullmatch(key) or any(marker in key.casefold() for marker in _SENSITIVE):
            _fail()
        value = payload.get("value")
        if type(value) not in {str, int, bool}:
            _fail()
        if type(value) is int and not -(2**31) <= value < 2**31:
            _fail()
        if isinstance(value, str) and (
            not value or value != value.strip() or len(value.encode()) > 2048
            or any(char in value for char in ("\x00", "\n", "\r"))
        ):
            _fail()
        normalized.update(key=key, value=value, reason=_trimmed_text(payload.get("reason"), 1, 512))
    else:
        normalized = _base(payload, common)

    destructive = operation in _DESTRUCTIVE
    if destructive:
        confirmation = _domain(payload.get("confirmation"))
        if confirmation != normalized["domain"] or payload.get("pre_operation_backup") is not True:
            _fail()
        normalized.update(confirmation=confirmation, pre_operation_backup=True)

    return PayloadAuthorization(
        normalized_payload=normalized,
        site_required=operation not in {"site.create", "site.create_blank", "site.create_from_backup"},
        destructive=destructive,
        pre_operation_backup=bool(normalized.get("pre_operation_backup", False)),
    )


__all__ = ["LIFECYCLE_AUTHORING_OPERATIONS", "validate_lifecycle_payload"]
