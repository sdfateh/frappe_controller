"""Strict dependency-free controller inventory reconciliation."""
from __future__ import annotations

import hashlib, json, re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Protocol, Sequence

_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BENCH = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DOMAIN = re.compile(r"^(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DATABASE = re.compile(r"^[a-z0-9_]{1,64}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_STATUSES = {"ready", "degraded", "error", "maintenance"}


class InventoryReconciliationError(RuntimeError): pass
class InventorySchemaError(InventoryReconciliationError): pass
class InventoryOwnershipError(InventoryReconciliationError): pass
class InventoryConflictError(InventoryReconciliationError): pass


def _obj(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise InventorySchemaError(f"{name} must be an object")
    return value


def _exact(value: Mapping[str, Any], fields: set[str], name: str) -> None:
    if set(value) != fields:
        raise InventorySchemaError(f"{name} fields do not match protocol v1")


def _text(value: object, name: str, limit: int = 253) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or "\n" in value or len(value.encode()) > limit:
        raise InventorySchemaError(f"{name} is invalid")
    return value


def _array(value: object, name: str, limit: int) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)) or len(value) > limit:
        raise InventorySchemaError(f"{name} must be a bounded array")
    return value


def _pairs(value: object, name: str, limit: int) -> tuple[tuple[str, str], ...]:
    result = []
    for index, item in enumerate(_array(value, name, limit)):
        pair = _array(item, f"{name}[{index}]", 2)
        if len(pair) != 2:
            raise InventorySchemaError(f"{name} entries require two values")
        result.append((_text(pair[0], name, 128), _text(pair[1], name, 256)))
    if result != sorted(result) or len({key for key, _ in result}) != len(result):
        raise InventorySchemaError(f"{name} must be sorted and unique")
    return tuple(result)


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InventorySchemaError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SiteSnapshot:
    domain: str
    apps: tuple[tuple[str, str], ...]
    scheduler_enabled: bool | None
    maintenance_mode: bool | None
    database_name: str


@dataclass(frozen=True, slots=True)
class BenchSnapshot:
    bench_id: str
    versions: tuple[tuple[str, str], ...]
    capabilities: tuple[str, ...]
    sites: tuple[SiteSnapshot, ...]


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    schema_version: int
    inventory_version: str
    benches: tuple[BenchSnapshot, ...]
    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class HeartbeatInventory:
    status: str
    version: str
    inventory_digest: str
    inventory: InventorySnapshot


@dataclass(frozen=True, slots=True)
class StoredInventory:
    digest: str
    benches: tuple[BenchSnapshot, ...]
    revision: int


@dataclass(frozen=True, slots=True)
class InventoryDelta:
    previous_digest: str | None
    current_digest: str
    revision: int
    added_benches: tuple[str, ...]
    removed_benches: tuple[str, ...]
    changed_benches: tuple[str, ...]
    added_sites: tuple[tuple[str, str], ...]
    removed_sites: tuple[tuple[str, str], ...]
    changed_sites: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class AgentProjection:
    agent_id: str
    reported_status: str
    effective_status: str
    version: str
    last_seen_at: datetime
    inventory_digest: str
    inventory_revision: int
    capabilities: tuple[str, ...]
    online: bool


class InventoryRepository(Protocol):
    def current_inventory(self, agent_id: str) -> StoredInventory | None: ...
    def bench_owner(self, bench_id: str) -> str | None: ...
    def site_owner(self, domain: str) -> tuple[str, str] | None: ...
    def replace_inventory(self, agent_id: str, heartbeat: HeartbeatInventory, *, previous_digest: str | None, revision: int, observed_at: datetime) -> None: ...
    def agent_projection(self, agent_id: str) -> AgentProjection | None: ...


def parse_heartbeat_state(value: object) -> HeartbeatInventory:
    raw = _obj(value, "state")
    _exact(raw, {"status", "version", "inventory_digest", "inventory"}, "state")
    status = _text(raw["status"], "state.status", 32)
    if status not in _STATUSES: raise InventorySchemaError("unsupported status")
    version = _text(raw["version"], "state.version", 64)
    digest = _text(raw["inventory_digest"], "inventory_digest", 64)
    if not _DIGEST.fullmatch(digest): raise InventorySchemaError("invalid inventory digest")
    snapshot = _parse_inventory(raw["inventory"])
    if snapshot.digest != digest: raise InventorySchemaError("inventory digest mismatch")
    return HeartbeatInventory(status, version, digest, snapshot)


def _parse_inventory(value: object) -> InventorySnapshot:
    raw = _obj(value, "inventory")
    _exact(raw, {"schema_version", "inventory_version", "benches"}, "inventory")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1: raise InventorySchemaError("schema_version must be 1")
    if raw["inventory_version"] != "1.0": raise InventorySchemaError("inventory_version must be 1.0")
    benches = tuple(_parse_bench(item, i) for i, item in enumerate(_array(raw["benches"], "benches", 256)))
    ids = [item.bench_id for item in benches]
    if ids != sorted(ids) or len(ids) != len(set(ids)): raise InventorySchemaError("benches must be sorted and unique")
    owners = {}
    for bench in benches:
        for site in bench.sites:
            if site.domain in owners: raise InventorySchemaError("duplicate site ownership")
            owners[site.domain] = bench.bench_id
    if len(owners) > 10_000: raise InventorySchemaError("too many sites")
    return InventorySnapshot(1, "1.0", benches)


def _parse_bench(value: object, index: int) -> BenchSnapshot:
    name = f"benches[{index}]"; raw = _obj(value, name)
    _exact(raw, {"bench_id", "versions", "capabilities", "sites"}, name)
    bench_id = _text(raw["bench_id"], f"{name}.bench_id", 64)
    if not _BENCH.fullmatch(bench_id): raise InventorySchemaError("invalid bench_id")
    caps = tuple(_text(item, "capability", 128) for item in _array(raw["capabilities"], "capabilities", 128))
    if caps != tuple(sorted(caps)) or len(caps) != len(set(caps)) or any(not _CAPABILITY.fullmatch(item) for item in caps): raise InventorySchemaError("capabilities must be sorted typed names")
    sites = tuple(_parse_site(item, name, i) for i, item in enumerate(_array(raw["sites"], "sites", 10_000)))
    domains = [item.domain for item in sites]
    if domains != sorted(domains) or len(domains) != len(set(domains)): raise InventorySchemaError("sites must be sorted and unique")
    return BenchSnapshot(bench_id, _pairs(raw["versions"], "versions", 128), caps, sites)


def _parse_site(value: object, parent: str, index: int) -> SiteSnapshot:
    name = f"{parent}.sites[{index}]"; raw = _obj(value, name)
    _exact(raw, {"domain", "apps", "scheduler_enabled", "maintenance_mode", "database_name"}, name)
    domain = _text(raw["domain"], "domain")
    if domain != domain.lower() or not _DOMAIN.fullmatch(domain): raise InventorySchemaError("invalid canonical domain")
    database = _text(raw["database_name"], "database_name", 64)
    if not _DATABASE.fullmatch(database): raise InventorySchemaError("invalid database_name")
    scheduler, maintenance = raw["scheduler_enabled"], raw["maintenance_mode"]
    if scheduler is not None and type(scheduler) is not bool: raise InventorySchemaError("invalid scheduler state")
    if maintenance is not None and type(maintenance) is not bool: raise InventorySchemaError("invalid maintenance state")
    return SiteSnapshot(domain, _pairs(raw["apps"], "apps", 256), scheduler, maintenance, database)


def _delta(previous: StoredInventory | None, current: InventorySnapshot, revision: int) -> InventoryDelta:
    old_b = {} if previous is None else {b.bench_id: b for b in previous.benches}; new_b = {b.bench_id: b for b in current.benches}
    old_s = {} if previous is None else {(b.bench_id, s.domain): s for b in previous.benches for s in b.sites}; new_s = {(b.bench_id, s.domain): s for b in current.benches for s in b.sites}
    return InventoryDelta(None if previous is None else previous.digest, current.digest, revision, tuple(sorted(new_b.keys()-old_b.keys())), tuple(sorted(old_b.keys()-new_b.keys())), tuple(sorted(k for k in old_b.keys()&new_b.keys() if old_b[k] != new_b[k])), tuple(sorted(new_s.keys()-old_s.keys())), tuple(sorted(old_s.keys()-new_s.keys())), tuple(sorted(k for k in old_s.keys()&new_s.keys() if old_s[k] != new_s[k])))


class InventoryReconciliationService:
    def __init__(self, repository: InventoryRepository, *, offline_after_seconds: int):
        if type(offline_after_seconds) is not int or offline_after_seconds < 1: raise ValueError("offline threshold must be positive")
        self._repository = repository; self._offline_after = timedelta(seconds=offline_after_seconds)
    def reconcile(self, agent_id: str, state: object, *, observed_at: datetime) -> InventoryDelta:
        agent_id = _text(agent_id, "agent_id", 128)
        if not _AGENT.fullmatch(agent_id): raise InventorySchemaError("invalid agent_id")
        observed = _utc(observed_at, "observed_at"); heartbeat = parse_heartbeat_state(state); previous = self._repository.current_inventory(agent_id)
        for bench in heartbeat.inventory.benches:
            owner = self._repository.bench_owner(bench.bench_id)
            if owner is not None and owner != agent_id: raise InventoryOwnershipError("bench owned by another agent")
            for site in bench.sites:
                owner_pair = self._repository.site_owner(site.domain)
                if owner_pair is not None and owner_pair != (agent_id, bench.bench_id): raise InventoryOwnershipError("site owned by another agent or bench")
        revision = 1 if previous is None else previous.revision + 1; delta = _delta(previous, heartbeat.inventory, revision)
        self._repository.replace_inventory(agent_id, heartbeat, previous_digest=None if previous is None else previous.digest, revision=revision, observed_at=observed)
        return delta
    def project_agent(self, agent_id: str, *, now: datetime) -> AgentProjection | None:
        projection = self._repository.agent_projection(agent_id)
        if projection is None: return None
        current, seen = _utc(now, "now"), _utc(projection.last_seen_at, "last_seen_at")
        if seen > current + timedelta(minutes=5): raise InventoryReconciliationError("future last_seen_at")
        online = current - seen < self._offline_after
        return AgentProjection(projection.agent_id, projection.reported_status, projection.reported_status if online else "offline", projection.version, seen, projection.inventory_digest, projection.inventory_revision, projection.capabilities, online)
