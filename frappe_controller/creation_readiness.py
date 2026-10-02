"""Read-only pre-creation gates. Never infer readiness from missing evidence."""

from __future__ import annotations

from datetime import UTC, datetime
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ReadinessError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _json(value: Any) -> Any:
    try:
        return json.loads(value or "null")
    except (TypeError, json.JSONDecodeError):
        return None


def _fresh(value: Any, now: datetime, maximum: int) -> bool:
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
        return 0 <= (now - stamp).total_seconds() <= maximum
    except (TypeError, ValueError):
        return False


def dns_preflight(settings: Any, domain: str) -> None:
    """GET-only zone scope/access and conflicting-record checks; never prove edit permission."""
    token, zone = settings.cloudflare_api_token, settings.cloudflare_zone_id
    if not isinstance(token, str) or len(token) < 20 or not isinstance(zone, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", zone):
        raise ReadinessError("Configure a valid Cloudflare token and zone in Controller Settings.")
    opener = build_opener(_NoRedirect())

    def get(path: str) -> Any:
        request = Request("https://api.cloudflare.com/client/v4/" + path, method="GET", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
        })
        try:
            with opener.open(request, timeout=8) as response:
                raw = response.read(1024 * 1024 + 1)
            value = json.loads(raw) if len(raw) <= 1024 * 1024 else None
        except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ReadinessError("Cloudflare read-only access check failed; check token, zone and connectivity.") from None
        if not isinstance(value, dict) or value.get("success") is not True:
            raise ReadinessError("Cloudflare did not confirm read-only access to the configured zone.")
        return value.get("result")

    zone_path = "zones/" + quote(zone, safe="")
    info = get(zone_path)
    name = info.get("name") if isinstance(info, dict) else None
    if not isinstance(name, str) or info.get("status") != "active" or not (domain == name or domain.endswith("." + name)):
        raise ReadinessError("The domain is not in the configured active Cloudflare zone.")
    records = get(zone_path + "/dns_records?" + urlencode({"name": domain, "per_page": 100}))
    if not isinstance(records, list):
        raise ReadinessError("Cloudflare returned invalid record evidence.")
    if any(not isinstance(row, dict) or row.get("type") in {"A", "AAAA", "CNAME"} for row in records):
        raise ReadinessError("DNS already has an address/alias record for this domain; reconcile ownership first.")


def ingress_preflight(address: str) -> None:
    try:
        ip = ipaddress.IPv4Address(address)
        if not ip.is_global:
            raise ValueError
        with socket.create_connection((str(ip), 443), timeout=4):
            pass
    except (ValueError, OSError):
        raise ReadinessError("The Agent must have a public IPv4 address with reachable HTTPS ingress on port 443.") from None


def check_creation_readiness(frappe: Any, *, domain: str, server_agent: str, bench: str, now: datetime | None = None) -> dict[str, Any]:
    from .controller_settings import load_controller_settings
    from .feature_flags import require_operation, target_environment
    from .frappe_operation_service import FrappeOperationAuthoringRepository
    from .operation_service import OperationAuthoringError, select_approval_rule
    now = now or datetime.now(UTC)
    checks: list[dict[str, Any]] = []

    def check(key: str, label: str, passed: bool, good: str, bad: str) -> None:
        checks.append({"key": key, "label": label, "passed": bool(passed), "message": good if passed else bad})

    agent = frappe.db.get_value("Server Agent", server_agent, [
        "enabled", "status", "reported_status", "last_seen", "inventory_updated_at", "drain_requested",
        "public_ip", "allowed_site_suffixes_json", "allowed_operations_json", "inventory_digest",
    ], as_dict=True)
    target = frappe.db.get_value("Bench", bench, [
        "enabled", "server_agent", "health_status", "inventory_updated_at", "installed_apps_json", "required_apps_json",
    ], as_dict=True)
    if not agent or not target:
        check("target", "Target", False, "", "Select an existing Agent and Bench.")
        return {"ready": False, "checks": checks, "required_apps": []}
    check("heartbeat", "Agent online", agent.enabled and agent.status == "Online" and agent.reported_status == "ready" and not agent.drain_requested and _fresh(agent.last_seen, now, 120),
          "Agent is ready with a heartbeat within 120 seconds.", "Agent is offline, unhealthy, draining, or its heartbeat is older than 120 seconds.")
    check("inventory", "Fresh target inventory", target.enabled and target.server_agent == server_agent and target.health_status == "healthy" and bool(agent.inventory_digest) and _fresh(agent.inventory_updated_at, now, 300) and _fresh(target.inventory_updated_at, now, 300),
          "Agent and target inventory are healthy and no older than 300 seconds.", "Target ownership, health, or fresh inventory is missing (maximum age 300 seconds).")
    suffixes, operations = _json(agent.allowed_site_suffixes_json), _json(agent.allowed_operations_json)
    allowed = isinstance(suffixes, list) and all(isinstance(item, str) and item for item in suffixes) and any(domain == item or domain.endswith("." + item) for item in suffixes)
    check("installation_policy", "Installation policy", allowed and isinstance(operations, list) and "site.create_blank" in operations,
          "Domain and site creation are allowed by this installation.", "Domain suffix or site creation is not allowed by the Agent installation policy.")
    try:
        environment = target_environment(frappe, server_agent, None)
        require_operation(frappe, environment=environment, operation_type="site.create_blank", payload_json=json.dumps({"domain": domain}))
        repository = FrappeOperationAuthoringRepository(frappe)
        snapshot = repository.resolve_target(server_agent, bench, None)
        policy = select_approval_rule(repository.approval_rules(), environment, "site.create_blank")
        permitted = snapshot.agent_enabled and "site.create_blank" in snapshot.capabilities and (environment != "production" or policy is not None) and not (policy and policy.require_backup)
        check("target_policy", "Target and approval policy", permitted, "Feature, capabilities and approval policies allow this target.", "Target capabilities or approval policy do not permit blank-site creation.")
    except (OperationAuthoringError, frappe.PermissionError):
        check("target_policy", "Target and approval policy", False, "", "Feature, target or approval policy prevents creation.")
    required, available = _json(target.required_apps_json), _json(target.installed_apps_json)
    valid_policy = isinstance(required, list) and bool(required) and all(isinstance(app, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", app) for app in required) and "frappe" in required and len(set(required)) == len(required)
    available_names = {row[0] for row in available if isinstance(row, list) and len(row) == 2 and isinstance(row[0], str) and isinstance(row[1], str) and row[1] not in {"", "unknown"}} if isinstance(available, list) else set()
    check("apps", "Required apps available", valid_policy and set(required).issubset(available_names),
          "Fresh bench inventory contains the Agent's required provisioning apps.", "Required-app policy or app version evidence is missing; update the Agent/policy/apps and wait for inventory.")
    check("domain_available", "Domain available", not frappe.db.exists("Managed Site", {"domain": domain}), "Domain is not already managed.", "A Managed Site already exists for this domain.")
    # Avoid outbound probes for invalid/stale targets, not arbitrary user-selected hosts.
    if all(row["passed"] for row in checks):
        for key, label, callback, success in [
            ("dns", "DNS provider", lambda: dns_preflight(load_controller_settings(frappe), domain), "Active zone and read access verified; no address conflict. DNS edit permission is checked on execution."),
            ("ingress", "Public HTTPS ingress", lambda: ingress_preflight(agent.public_ip), "Public IPv4 port 443 is reachable. Agent rechecks local routing before effects; final gate verifies public HTTPS."),
        ]:
            try:
                callback()
                check(key, label, True, success, "")
            except ReadinessError as exc:
                check(key, label, False, "", str(exc))
            except Exception:
                # Never leak provider credentials or configuration through exception text.
                check(key, label, False, "", "Read-only check failed; verify DNS configuration/zone access and public ingress connectivity." if key == "dns" else "Public IPv4 HTTPS port 443 is unreachable or invalid.")
    else:
        for key, label in [("dns", "DNS provider"), ("ingress", "Public HTTPS ingress")]:
            check(key, label, False, "", "Not checked until target, policy and inventory checks pass.")
    return {"ready": all(row["passed"] for row in checks), "checks": checks, "required_apps": required if valid_policy else []}


def creation_handover_ready(result_json: Any, credential_received_at: Any, expected_apps_json: Any = None) -> bool:
    value = _json(result_json)
    result = value.get("result") if isinstance(value, dict) else None
    readiness = result.get("readiness") if isinstance(result, dict) else None
    apps = readiness.get("required_apps") if isinstance(readiness, dict) else None
    valid = bool(credential_received_at and isinstance(readiness, dict) and type(readiness.get("version")) is int and readiness["version"] == 1 and readiness.get("apps_verified") is True and readiness.get("public_https_verified") is True and isinstance(apps, list) and apps and "frappe" in apps and all(isinstance(app, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", app) for app in apps))
    if not valid or len(set(apps)) != len(apps):
        return False
    if expected_apps_json is not None:
        expected = _json(expected_apps_json)
        return isinstance(expected, list) and bool(expected) and all(isinstance(app, str) for app in expected) and set(expected) == set(apps)
    return True
