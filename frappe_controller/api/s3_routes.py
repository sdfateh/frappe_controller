"""Controller-owned, operation-bound S3 authorization routes."""

from __future__ import annotations

import base64
import json
import re
from pathlib import PurePosixPath
from typing import Any, Mapping

import boto3
import frappe
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from werkzeug.wrappers import Response

from ..agent_request_auth import trusted_peer_and_route_from_frappe_request
from ..security import ControllerRequestError, canonical_json, require_agent_id
from ..controller_settings import (
    ControllerSettings,
    ControllerSettingsError,
    load_controller_settings,
)
from ..frappe_security_store import FrappeCertificateStore


_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_RESTORE_OPERATIONS = frozenset({
    "site.create", "site.create_from_backup", "site.restore", "site.reinstall"
})
_HEAD_FIELDS = (
    "ContentLength", "ETag", "VersionId", "ChecksumCRC64NVME",
    "ChecksumCRC32C", "ChecksumCRC32", "ChecksumSHA256", "ChecksumSHA1",
)
_CHECKSUM_HEADERS = (
    ("ChecksumCRC64NVME", "x-amz-checksum-crc64nvme"),
    ("ChecksumCRC32C", "x-amz-checksum-crc32c"),
    ("ChecksumCRC32", "x-amz-checksum-crc32"),
    ("ChecksumSHA256", "x-amz-checksum-sha256"),
    ("ChecksumSHA1", "x-amz-checksum-sha1"),
)


def _response(value: Mapping[str, Any], status: int = 200) -> Response:
    return Response(
        canonical_json(value), status=status,
        content_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _body() -> dict[str, Any]:
    request = frappe.request
    if request.method != "POST" or request.mimetype != "application/json":
        raise ControllerRequestError("invalid_s3_request", 415)
    raw = request.get_data(cache=True)
    if len(raw) > 64 * 1024:
        raise ControllerRequestError("s3_request_too_large", 413)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ControllerRequestError("invalid_s3_request", 400) from None
    if not isinstance(value, dict) or set(value) != {
        "agent_id", "operation_id", "action", "parameters"
    } or not isinstance(value["parameters"], dict):
        raise ControllerRequestError("invalid_s3_request", 400)
    return value


def _normalize_prefix(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or "\\" in value:
        raise ControllerRequestError("s3_scope_invalid", 409)
    parts = value.strip("/").split("/")
    if any(not _SAFE_SEGMENT.fullmatch(part) for part in parts):
        raise ControllerRequestError("s3_scope_invalid", 409)
    return "/".join(parts)


def _operation(value: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    try:
        agent_id = require_agent_id(value.get("agent_id"))
    except ValueError:
        raise ControllerRequestError("invalid_agent", 403) from None
    operation_id = value.get("operation_id")
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ControllerRequestError("invalid_s3_request", 400)
    peer, routed_agent = trusted_peer_and_route_from_frappe_request(frappe.request, "s3")
    if peer.agent_id != agent_id or routed_agent != agent_id:
        raise ControllerRequestError("agent_binding_mismatch", 403)
    FrappeCertificateStore.from_environment(
        frappe_module=frappe
    ).authenticate_peer(peer, routed_agent, agent_id)
    row = frappe.db.get_value(
        "Operation", operation_id,
        ["server_agent", "operation_type", "payload_json", "state"], as_dict=True,
    )
    if not row or row.server_agent != agent_id or row.operation_type not in _RESTORE_OPERATIONS:
        raise ControllerRequestError("s3_operation_not_owned", 403)
    # Queued is already approved and immutable. The Agent may receive the command
    # before the lease projection is visible to this separate request.
    if row.state not in {"queued", "leased", "running"}:
        raise ControllerRequestError("s3_operation_not_active", 409)
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, json.JSONDecodeError):
        raise ControllerRequestError("s3_operation_invalid", 409) from None
    if not isinstance(payload, dict):
        raise ControllerRequestError("s3_operation_invalid", 409)
    template = payload.get("template_name")
    if not isinstance(template, str) or not _SAFE_SEGMENT.fullmatch(template):
        raise ControllerRequestError("s3_operation_invalid", 409)
    parent = _normalize_prefix(payload.get("backup_prefix"))
    return payload, agent_id, "/".join(part for part in (parent, template) if part)


def _configured_buckets(settings: ControllerSettings) -> tuple[str, ...]:
    if not settings.s3_allowed_buckets:
        raise ControllerRequestError("controller_s3_not_configured", 503)
    return settings.s3_allowed_buckets


def _scope(
    parameters: Mapping[str, Any], payload: Mapping[str, Any], prefix: str,
    settings: ControllerSettings,
) -> tuple[str, str | None, str | None]:
    supplied_bucket = parameters.get("Bucket")
    requested_bucket = payload.get("bucket_name") or settings.s3_bucket
    if supplied_bucket == "@controller-default":
        supplied_bucket = settings.s3_bucket
    if (
        supplied_bucket != requested_bucket
        or supplied_bucket not in _configured_buckets(settings)
    ):
        raise ControllerRequestError("s3_bucket_not_allowed", 403)
    parent = _normalize_prefix(payload.get("backup_prefix"))
    if parent not in settings.s3_allowed_prefixes:
        raise ControllerRequestError("s3_prefix_not_allowed", 403)
    bucket = supplied_bucket
    key = parameters.get("Key")
    request_prefix = parameters.get("Prefix")
    if key is not None:
        if (
            not isinstance(key, str) or not key.startswith(f"{prefix}/")
            or "\\" in key or any(part in {"", ".", ".."} for part in key.split("/"))
        ):
            raise ControllerRequestError("s3_key_not_allowed", 403)
    if request_prefix is not None and request_prefix != f"{prefix}/":
        raise ControllerRequestError("s3_prefix_not_allowed", 403)
    return bucket, key, request_prefix


def _client(settings: ControllerSettings):
    access_key = settings.aws_access_key_id
    secret_key = settings.aws_secret_access_key
    if bool(access_key) != bool(secret_key):
        raise ControllerRequestError("controller_aws_credentials_incomplete", 503)
    options: dict[str, Any] = {
        "region_name": settings.aws_region,
        "config": Config(
            response_checksum_validation="when_supported",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    }
    if access_key:
        options.update(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    return boto3.client("s3", **options)


def _head_result(head: Mapping[str, Any]) -> dict[str, Any]:
    return {field: head[field] for field in _HEAD_FIELDS if field in head}


def _execute(
    action: str, parameters: Mapping[str, Any], payload: Mapping[str, Any],
    prefix: str, settings: ControllerSettings,
) -> dict[str, Any]:
    if action == "get_restore_policy":
        if parameters:
            raise ControllerRequestError("invalid_s3_parameters", 400)
        return {
            "MaxObjectBytes": settings.s3_max_object_bytes,
            "MaxRestoreBytes": settings.s3_max_restore_bytes,
        }
    bucket, key, request_prefix = _scope(parameters, payload, prefix, settings)
    client = _client(settings)
    if action == "get_bucket_versioning":
        if set(parameters) != {"Bucket"}:
            raise ControllerRequestError("invalid_s3_parameters", 400)
        response = client.get_bucket_versioning(Bucket=bucket)
        return {"Status": response.get("Status")}
    if action == "list_object_versions":
        if set(parameters) - {"Bucket", "Prefix"} or request_prefix is None:
            raise ControllerRequestError("invalid_s3_parameters", 400)
        versions: list[dict[str, Any]] = []
        markers: list[dict[str, Any]] = []
        for page in client.get_paginator("list_object_versions").paginate(
            Bucket=bucket, Prefix=request_prefix
        ):
            versions.extend({field: item.get(field) for field in ("Key", "VersionId", "IsLatest", "Size", "ETag")} for item in page.get("Versions", []))
            markers.extend({field: item.get(field) for field in ("Key", "VersionId", "IsLatest")} for item in page.get("DeleteMarkers", []))
            if len(versions) + len(markers) > 10_000:
                raise ControllerRequestError("s3_listing_too_large", 409)
        return {"Versions": versions, "DeleteMarkers": markers}
    if action in {"head_object", "get_manifest", "presign_get_object"}:
        allowed = {"Bucket", "Key", "VersionId", "ChecksumMode"}
        if set(parameters) - allowed or key is None:
            raise ControllerRequestError("invalid_s3_parameters", 400)
        version = parameters.get("VersionId")
        if not isinstance(version, str) or not version or version == "null":
            raise ControllerRequestError("s3_version_required", 400)
        head = client.head_object(
            Bucket=bucket, Key=key, VersionId=version, ChecksumMode="ENABLED"
        )
        if int(head.get("ContentLength", -1)) > settings.s3_max_object_bytes:
            raise ControllerRequestError("s3_object_too_large", 409)
        result = _head_result(head)
        if action == "head_object":
            return result
        if action == "get_manifest":
            if PurePosixPath(key).name != "_frappe_backup_complete.v1.json" or int(head.get("ContentLength", -1)) > 1024 * 1024:
                raise ControllerRequestError("s3_manifest_not_allowed", 403)
            response = client.get_object(
                Bucket=bucket, Key=key, VersionId=version, ChecksumMode="ENABLED"
            )
            body = response["Body"]
            try:
                raw = body.read(1024 * 1024 + 1)
            finally:
                body.close()
            if len(raw) > 1024 * 1024:
                raise ControllerRequestError("s3_manifest_too_large", 409)
            return {**_head_result(response), "BodyBase64": base64.b64encode(raw).decode("ascii")}
        checksum = next(
            ((field, header, head.get(field)) for field, header in _CHECKSUM_HEADERS if head.get(field)),
            None,
        )
        if checksum is None:
            raise ControllerRequestError("s3_checksum_required", 409)
        _field, header, checksum_value = checksum
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key, "VersionId": version, "ChecksumMode": "ENABLED"},
            ExpiresIn=settings.s3_presigned_url_seconds,
        )
        return {
            "URL": url, "ContentLength": int(head["ContentLength"]),
            "ChecksumHeader": header, "ChecksumValue": checksum_value,
        }
    raise ControllerRequestError("unsupported_s3_action", 400)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def storage_route(**_request_arguments: Any) -> Response:
    try:
        value = _body()
        payload, _agent_id, prefix = _operation(value)
        settings = load_controller_settings(frappe)
        action = value["action"]
        if not isinstance(action, str):
            raise ControllerRequestError("invalid_s3_request", 400)
        result = _execute(action, value["parameters"], payload, prefix, settings)
        return _response({"accepted": True, "result": result})
    except ControllerRequestError as error:
        frappe.db.rollback()
        return _response({"accepted": False, "error": error.code}, error.status)
    except (BotoCoreError, ClientError, ControllerSettingsError):
        frappe.db.rollback()
        return _response({"accepted": False, "error": "s3_provider_failed"}, 502)
    except Exception:
        frappe.db.rollback()
        return _response({"accepted": False, "error": "internal_error"}, 500)
