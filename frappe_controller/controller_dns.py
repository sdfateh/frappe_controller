"""Controller-owned Cloudflare DNS client with strict response validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ControllerDNSError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudflareDNSConfig:
    api_token: str
    zone_id: str
    timeout_seconds: float = 20.0
    proxied: bool = False

    def validate(self) -> None:
        if not isinstance(self.api_token, str) or len(self.api_token) < 20:
            raise ControllerDNSError("Cloudflare API token is not configured")
        if not isinstance(self.zone_id, str) or not self.zone_id or "/" in self.zone_id:
            raise ControllerDNSError("Cloudflare zone ID is not configured")
        if not 1 <= self.timeout_seconds <= 120:
            raise ControllerDNSError("Cloudflare timeout is invalid")


class CloudflareDNS:
    def __init__(
        self,
        config: CloudflareDNSConfig,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        config.validate()
        self.config = config
        self._opener = opener
        zone = quote(config.zone_id, safe="")
        self._records_url = f"https://api.cloudflare.com/client/v4/zones/{zone}/dns_records"

    def _request(
        self, method: str, url: str, body: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        encoded = None if body is None else json.dumps(
            body, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        request = Request(
            url,
            data=encoded,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(1024 * 1024 + 1)
        except (HTTPError, URLError, OSError):
            raise ControllerDNSError("Cloudflare request failed") from None
        if len(raw) > 1024 * 1024:
            raise ControllerDNSError("Cloudflare response exceeded its size limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControllerDNSError("Cloudflare returned invalid JSON") from None
        if not isinstance(value, Mapping) or value.get("success") is not True:
            raise ControllerDNSError("Cloudflare rejected the DNS request")
        result = value.get("result")
        if not isinstance(result, Mapping):
            raise ControllerDNSError("Cloudflare response is missing DNS evidence")
        return result

    def create_a_record(self, domain: str, address: str) -> str:
        result = self._request(
            "POST",
            self._records_url,
            {
                "type": "A", "name": domain, "content": address,
                "ttl": 1, "proxied": self.config.proxied,
            },
        )
        record_id = result.get("id")
        if not isinstance(record_id, str) or not record_id or len(record_id) > 255:
            raise ControllerDNSError("Cloudflare did not return a DNS record ID")
        return record_id

    def delete_owned_record(self, record_id: str, expected_domain: str) -> None:
        escaped = quote(record_id, safe="")
        current = self._request("GET", f"{self._records_url}/{escaped}")
        if current.get("id") != record_id or current.get("name") != expected_domain:
            raise ControllerDNSError("Cloudflare DNS record ownership does not match")
        result = self._request("DELETE", f"{self._records_url}/{escaped}")
        if result.get("id") != record_id:
            raise ControllerDNSError("Cloudflare did not confirm DNS record deletion")
