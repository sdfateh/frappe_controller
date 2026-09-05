from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import frappe
from frappe.model.document import Document


class FrappeControllerSettings(Document):
    def validate(self):
        public_url = (self.public_controller_url or "").strip().rstrip("/")
        if public_url:
            parsed = urlsplit(public_url)
            if (
                parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            ):
                frappe.throw("Public Controller URL must be a plain HTTPS origin", frappe.ValidationError)
        self.public_controller_url = public_url
        if not 60 <= int(self.enrollment_token_ttl_seconds or 0) <= 3600:
            frappe.throw(
                "Enrollment token lifetime must be between 60 and 3600 seconds",
                frappe.ValidationError,
            )
        image = (self.agent_image_reference or "").strip()
        if image and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}@sha256:[0-9a-f]{64}", image):
            frappe.throw("Agent image reference must use an immutable SHA-256 digest", frappe.ValidationError)
        self.agent_image_reference = image
        for fieldname in ("s3_allowed_buckets", "s3_allowed_prefixes"):
            try:
                value = json.loads(self.get(fieldname) or "[]")
            except json.JSONDecodeError:
                frappe.throw(f"{fieldname} must be valid JSON", frappe.ValidationError)
            if not isinstance(value, list) or any(
                not isinstance(item, str) or item != item.strip() for item in value
            ):
                frappe.throw(f"{fieldname} must be a JSON list of strings", frappe.ValidationError)
        access_key = bool(self.get("aws_access_key_id"))
        secret_key = bool(self.get("aws_secret_access_key"))
        if access_key != secret_key:
            frappe.throw(
                "AWS Access Key ID and AWS Secret Access Key must be set together",
                frappe.ValidationError,
            )
        if not 60 <= int(self.s3_presigned_url_seconds or 0) <= 900:
            frappe.throw(
                "Presigned URL lifetime must be between 60 and 900 seconds",
                frappe.ValidationError,
            )
        if int(self.s3_max_object_bytes or 0) < 1:
            frappe.throw("Maximum object bytes must be positive", frappe.ValidationError)
        if int(self.s3_max_restore_bytes or 0) < int(self.s3_max_object_bytes or 0):
            frappe.throw(
                "Maximum restore bytes cannot be smaller than maximum object bytes",
                frappe.ValidationError,
            )
        if (
            int(self.s3_max_object_bytes or 0) > 21474836480
            or int(self.s3_max_restore_bytes or 0) > 42949672960
        ):
            frappe.throw(
                "S3 byte limits cannot exceed the Agent safety ceilings (20 GiB per object, 40 GiB per restore)",
                frappe.ValidationError,
            )
