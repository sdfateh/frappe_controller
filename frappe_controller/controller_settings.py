"""Validated access to the encrypted Single DocType configuration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class ControllerSettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControllerSettings:
    public_controller_url: str
    agent_image_reference: str
    enrollment_token_ttl_seconds: int
    cloudflare_api_token: str
    cloudflare_zone_id: str
    cloudflare_proxied: bool
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str
    s3_bucket: str
    s3_allowed_buckets: tuple[str, ...]
    s3_allowed_prefixes: tuple[str, ...]
    s3_presigned_url_seconds: int
    s3_max_object_bytes: int
    s3_max_restore_bytes: int


def _list(value: Any, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        raise ControllerSettingsError(f"{field} is invalid") from None
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or item != item.strip() for item in parsed
    ):
        raise ControllerSettingsError(f"{field} is invalid")
    return tuple(dict.fromkeys(parsed))


def _password(document: Any, field: str) -> str:
    # Avoid decrypting an unset Password field so AWS and Cloudflare can be
    # configured independently.
    if not document.get(field):
        return ""
    try:
        return document.get_password(field) or ""
    except Exception:
        raise ControllerSettingsError(f"{field} could not be decrypted") from None


def load_controller_settings(frappe_module: Any) -> ControllerSettings:
    document = frappe_module.get_single("Frappe Controller Settings")
    cloudflare_token = _password(document, "cloudflare_api_token")
    cloudflare_zone = _password(document, "cloudflare_zone_id")
    aws_access_key = _password(document, "aws_access_key_id")
    aws_secret_key = _password(document, "aws_secret_access_key")
    allowed_buckets = _list(document.s3_allowed_buckets, "s3_allowed_buckets")
    default_bucket = (document.s3_bucket or "").strip()
    if default_bucket and default_bucket not in allowed_buckets:
        allowed_buckets = (default_bucket, *allowed_buckets)
    values = ControllerSettings(
        public_controller_url=(document.public_controller_url or "").strip().rstrip("/"),
        agent_image_reference=(document.agent_image_reference or "").strip(),
        enrollment_token_ttl_seconds=int(document.enrollment_token_ttl_seconds or 600),
        cloudflare_api_token=cloudflare_token,
        cloudflare_zone_id=cloudflare_zone,
        cloudflare_proxied=bool(document.cloudflare_proxied),
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        aws_region=(document.aws_region or "us-east-1").strip(),
        s3_bucket=default_bucket,
        s3_allowed_buckets=allowed_buckets,
        s3_allowed_prefixes=_list(document.s3_allowed_prefixes, "s3_allowed_prefixes"),
        s3_presigned_url_seconds=int(document.s3_presigned_url_seconds or 300),
        s3_max_object_bytes=int(document.s3_max_object_bytes or 21474836480),
        s3_max_restore_bytes=int(document.s3_max_restore_bytes or 42949672960),
    )
    if not 60 <= values.s3_presigned_url_seconds <= 900:
        raise ControllerSettingsError("presigned URL lifetime is invalid")
    if not 60 <= values.enrollment_token_ttl_seconds <= 3600:
        raise ControllerSettingsError("enrollment token lifetime is invalid")
    if values.public_controller_url:
        parsed = urlsplit(values.public_controller_url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        ):
            raise ControllerSettingsError("public Controller URL must be a plain HTTPS origin")
    if values.agent_image_reference and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}@sha256:[0-9a-f]{64}", values.agent_image_reference
    ):
        raise ControllerSettingsError("agent image reference must use an immutable SHA-256 digest")
    if values.s3_max_object_bytes < 1 or values.s3_max_restore_bytes < values.s3_max_object_bytes:
        raise ControllerSettingsError("S3 byte limits are invalid")
    if values.s3_max_object_bytes > 21474836480 or values.s3_max_restore_bytes > 42949672960:
        raise ControllerSettingsError("S3 byte limits exceed the agent safety ceiling")
    return values
