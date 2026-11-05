"""Read-only readiness evidence for a single authenticated installation."""

import json
from datetime import UTC, datetime


def installation_checks(agent, bench, *, agent_id: str, apps: list[str], now: datetime) -> dict[str, bool]:
    def fresh(value, seconds):
        if not isinstance(value, datetime):
            return False
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return 0 <= (now - value).total_seconds() <= seconds

    checks = {
        "agent_ready": bool(agent and agent.enabled and agent.status == "Online" and agent.reported_status == "ready" and not agent.drain_requested),
        "signed_heartbeat": bool(agent and fresh(agent.last_seen, 120)),
        "agent_inventory": bool(agent and agent.inventory_digest and fresh(agent.inventory_updated_at, 300)),
        "bench_inventory": bool(bench and bench.enabled and bench.server_agent == agent_id and bench.health_status == "healthy" and fresh(bench.inventory_updated_at, 300)),
        "required_apps": False,
    }
    if bench and bench.server_agent == agent_id:
        try:
            available = dict(json.loads(bench.installed_apps_json or "[]"))
            checks["required_apps"] = (
                bool(apps) and json.loads(bench.required_apps_json or "[]") == apps
                and all(isinstance(available.get(app), str) and available[app] not in {"", "unknown"} for app in apps)
            )
        except (ValueError, TypeError):
            pass
    return checks
