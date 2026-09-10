"""Approval-bound conversion of Frappe operations into protocol envelopes."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

try:
    from .security import canonical_json, timestamp, validate_json
except ImportError:  # pragma: no cover
    from security import canonical_json, timestamp, validate_json  # type: ignore

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
class DispatchError(RuntimeError):
    pass


class ApprovalNotSatisfied(DispatchError):
    pass


class DispatchConflict(DispatchError):
    pass


def _runtime() -> Any:
    import frappe

    return frappe


def _first(rows: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return rows[0] if rows else None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("dispatch time must be a datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def immutable_command(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in envelope.items() if key not in {"issued_at", "expires_at"}}


def immutable_command_hash(envelope: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(immutable_command(envelope)).encode()).hexdigest()


class ApprovalBoundDispatcher:
    """Locks and validates Operation/Target/Approval rows before queueing."""

    def __init__(
        self,
        frappe_module: Any | None = None,
        *,
        command_lifetime_seconds: int = 300,
    ) -> None:
        if command_lifetime_seconds < 1 or command_lifetime_seconds > 300:
            raise ValueError("command lifetime must be between 1 and 300 seconds")
        self.frappe = frappe_module or _runtime()
        self.db = self.frappe.db
        self.command_lifetime_seconds = command_lifetime_seconds

    def _operation(self, operation_name: str, *, lock: bool) -> Mapping[str, Any] | None:
        suffix = " FOR UPDATE" if lock else ""
        return _first(
            self.db.sql(
                "SELECT o.*, a.agent_id, a.audience, a.enabled AS agent_enabled, "
                "a.inventory_digest AS current_inventory_revision, "
                "a.drain_requested AS agent_draining, "
                "b.bench_id, s.site_id, s.domain AS site_domain "
                "FROM `tabOperation` o JOIN `tabServer Agent` a ON a.name=o.server_agent "
                "JOIN `tabBench` b ON b.name=o.bench LEFT JOIN `tabManaged Site` s ON s.name=o.managed_site "
                f"WHERE o.name=%s{suffix}",
                (operation_name,),
                as_dict=True,
            )
        )

    def _target(self, operation_name: str) -> Mapping[str, Any]:
        targets = self.db.sql(
            "SELECT name, operation, target_key, server_agent, bench, managed_site, "
            "site_domain_snapshot, operation_type_snapshot, payload_hash_snapshot, "
            "inventory_revision_snapshot, state "
            "FROM `tabOperation Target` WHERE operation=%s FOR UPDATE",
            (operation_name,),
            as_dict=True,
        )
        if len(targets) != 1:
            raise DispatchConflict("an operation must have exactly one immutable target")
        return targets[0]

    def _approval_claims(
        self, operation: Mapping[str, Any], target: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        if operation.get("bulk_parent"):
            return self._bulk_approval_claims(operation)
        rows = self.db.sql(
            "SELECT name, approval_policy, decision, approver, decided_at, decision_hash, "
            "payload_hash_snapshot, target_key_snapshot, preview_result_hash_snapshot "
            "FROM `tabOperation Approval` WHERE operation=%s ORDER BY decided_at, name FOR UPDATE",
            (operation["name"],),
            as_dict=True,
        )
        if any(row["decision"] == "rejected" for row in rows):
            raise ApprovalNotSatisfied("operation has a rejection")
        approved = [row for row in rows if row["decision"] == "approved"]
        required = int(operation.get("required_approvals") or 0)
        status = operation.get("approval_status")
        if required == 0:
            if status != "not_required":
                raise ApprovalNotSatisfied("approval bypass is inconsistent")
            return []
        if status != "approved" or len(approved) < required:
            raise ApprovalNotSatisfied("approval threshold is not satisfied")
        if int(operation.get("approval_count") or 0) != len(approved):
            raise DispatchConflict("stored approval count is stale")
        approvers: set[str] = set()
        claims: list[dict[str, Any]] = []
        for approval in approved:
            if approval["approval_policy"] != operation.get("approval_policy"):
                raise DispatchConflict("approval policy does not match operation")
            if approval["approver"] == operation["requested_by"] or approval["approver"] in approvers:
                raise DispatchConflict("approval attribution is invalid")
            digest = approval.get("decision_hash") or ""
            if not _SHA256.fullmatch(digest):
                raise DispatchConflict("approval decision hash is invalid")
            if (
                approval.get("payload_hash_snapshot") != operation["payload_hash"]
                or approval.get("target_key_snapshot") != target["target_key"]
                or (approval.get("preview_result_hash_snapshot") or None)
                != (operation.get("preview_result_hash") or None)
            ):
                raise DispatchConflict("approval evidence snapshot changed")
            approvers.add(approval["approver"])
            decided_at = approval["decided_at"]
            claims.append(
                {
                    "approval_id": approval["name"],
                    "approved_by": approval["approver"],
                    "decision_hash": digest,
                    "decided_at": timestamp(_utc(decided_at)),
                }
            )
        return claims

    def _bulk_approval_claims(
        self, operation: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        parents = self.db.sql(
            "SELECT p.name, p.requested_by, p.approval_policy, p.approval_status, "
            "p.required_approvals, p.approval_count, p.operation_type, p.payload_hash, "
            "p.snapshot_hash, p.dry_run_hash, p.state, t.preview_result_hash "
            "FROM `tabBulk Operation` p JOIN `tabBulk Operation Target` t "
            "ON t.bulk_operation=p.name WHERE p.name=%s AND t.name=%s FOR UPDATE",
            (operation["bulk_parent"], operation.get("bulk_target")),
            as_dict=True,
        )
        parent = _first(parents)
        if (
            parent is None
            or parent["requested_by"] != operation["requested_by"]
            or parent["approval_policy"] != operation.get("approval_policy")
            or parent["operation_type"] != operation["operation_type"]
            or parent["payload_hash"] != operation["payload_hash"]
            or parent.get("preview_result_hash") != operation.get("preview_result_hash")
            or parent["approval_status"] != "approved"
            or parent["state"] not in {"approved", "canary", "running", "paused"}
            or int(parent["required_approvals"] or 0)
            != int(operation.get("required_approvals") or 0)
            or int(parent["approval_count"] or 0)
            != int(operation.get("approval_count") or 0)
        ):
            raise ApprovalNotSatisfied("bulk approval binding is not satisfied")
        for field in ("snapshot_hash", "payload_hash", "dry_run_hash"):
            if not _SHA256.fullmatch(parent.get(field) or ""):
                raise DispatchConflict("bulk approval evidence hash is invalid")
        rows = self.db.sql(
            "SELECT name, approval_policy, decision, approver, decided_at, decision_hash, "
            "snapshot_hash, payload_hash, dry_run_hash "
            "FROM `tabBulk Operation Approval` WHERE bulk_operation=%s "
            "ORDER BY decided_at, name FOR UPDATE",
            (operation["bulk_parent"],),
            as_dict=True,
        )
        if any(row["decision"] == "rejected" for row in rows):
            raise ApprovalNotSatisfied("bulk operation has a rejection")
        approved = [row for row in rows if row["decision"] == "approved"]
        required = int(parent["required_approvals"] or 0)
        if required < 1 or len(approved) < required or len(approved) != int(
            parent["approval_count"] or 0
        ):
            raise ApprovalNotSatisfied("bulk approval threshold is not satisfied")
        claims: list[dict[str, Any]] = []
        approvers: set[str] = set()
        for approval in approved:
            if (
                approval["approval_policy"] != parent["approval_policy"]
                or approval["approver"] == parent["requested_by"]
                or approval["approver"] in approvers
                or approval.get("snapshot_hash") != parent["snapshot_hash"]
                or approval.get("payload_hash") != parent["payload_hash"]
                or approval.get("dry_run_hash") != parent["dry_run_hash"]
                or not _SHA256.fullmatch(approval.get("decision_hash") or "")
            ):
                raise DispatchConflict("bulk approval attribution or evidence changed")
            approvers.add(approval["approver"])
            claims.append({
                "approval_id": approval["name"],
                "approved_by": approval["approver"],
                "decision_hash": approval["decision_hash"],
                "decided_at": timestamp(_utc(approval["decided_at"])),
            })
        return claims

    def envelope(self, operation_name: str, *, now: datetime, lock: bool = True) -> dict[str, Any]:
        current = _utc(now)
        operation = self._operation(operation_name, lock=lock)
        if operation is None:
            raise DispatchConflict("unknown operation")
        if not operation.get("agent_enabled"):
            raise DispatchConflict("operation targets a disabled agent")
        if operation.get("agent_draining"):
            raise DispatchConflict("operation targets a draining agent")
        if operation["protocol_version"] != "1.0" or operation["audience"] != "frappe-controller":
            raise DispatchConflict("operation protocol identity is invalid")
        try:
            operation_id = str(uuid.UUID(operation["operation_id"]))
        except (ValueError, TypeError, AttributeError):
            raise DispatchConflict("operation UUID is invalid") from None
        if operation_id != operation["operation_id"].lower():
            raise DispatchConflict("operation UUID is not canonical")
        if not str(operation["idempotency_key"]).endswith(operation_id):
            raise DispatchConflict("idempotency key is not bound to the operation")
        try:
            payload = json.loads(operation["payload_json"])
        except (TypeError, json.JSONDecodeError):
            raise DispatchConflict("operation payload is invalid JSON") from None
        if not isinstance(payload, Mapping):
            raise DispatchConflict("operation payload must be an object")
        try:
            validate_json(payload, reject_sensitive=True)
        except (TypeError, ValueError):
            raise DispatchConflict("operation payload contains forbidden data") from None
        # Authoring rejects secret-bearing payloads before persistence.  Hash the
        # complete canonical object here: omitting nested secret-shaped keys would
        # allow approval claims to bind a different payload than the agent sees.
        payload_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        if not hmac.compare_digest(payload_hash, operation["payload_hash"]):
            raise DispatchConflict("operation payload hash changed")

        target = self._target(operation["name"])
        expected_target = (
            operation["server_agent"], operation["bench"], operation.get("managed_site"),
            operation.get("site_domain"), operation["operation_type"], operation["payload_hash"],
        )
        actual_target = (
            target["server_agent"], target["bench"], target.get("managed_site"),
            target.get("site_domain_snapshot"), target["operation_type_snapshot"], target["payload_hash_snapshot"],
        )
        if actual_target != expected_target:
            raise DispatchConflict("operation target snapshot changed")
        inventory_revision = target.get("inventory_revision_snapshot") or None
        if inventory_revision is not None and (
            not _SHA256.fullmatch(inventory_revision)
            or inventory_revision != operation.get("current_inventory_revision")
        ):
            raise DispatchConflict("operation inventory snapshot changed")
        claims = self._approval_claims(operation, target)
        return {
            "protocol_version": "1.0",
            "operation_id": operation_id,
            "idempotency_key": operation["idempotency_key"],
            "agent_id": operation["agent_id"],
            "audience": operation["audience"],
            "bench_id": operation["bench_id"],
            "site_id": operation.get("site_id"),
            "operation": operation["operation_type"],
            "payload": dict(payload),
            "requested_by": operation["requested_by"],
            "approval_claims": claims,
            "issued_at": timestamp(current),
            "expires_at": timestamp(current + timedelta(seconds=self.command_lifetime_seconds)),
            "payload_hash": payload_hash,
        }

    def dispatch(self, operation_name: str, *, now: datetime) -> dict[str, Any]:
        envelope = self.envelope(operation_name, now=now, lock=True)
        operation = self._operation(operation_name, lock=False)
        assert operation is not None
        digest = immutable_command_hash(envelope)
        existing_digest = operation.get("command_hash") or None
        if operation["state"] == "queued" and existing_digest == digest:
            return envelope
        if operation["state"] != "awaiting_approval" or existing_digest is not None:
            raise DispatchConflict("operation is not dispatchable")
        self.db.set_value(
            "Operation",
            operation_name,
            {"state": "queued", "command_json": canonical_json(envelope), "command_hash": digest},
            update_modified=False,
        )
        self.db.set_value("Operation Target", {"operation": operation_name}, "state", "queued", update_modified=False)
        return envelope

    def validate_persisted(self, operation_name: str, command_json: str, command_hash: str, *, now: datetime) -> dict[str, Any]:
        try:
            persisted = json.loads(command_json)
        except (TypeError, json.JSONDecodeError):
            raise DispatchConflict("persisted command is invalid") from None
        if not isinstance(persisted, Mapping):
            raise DispatchConflict("persisted command must be an object")
        if (
            not _SHA256.fullmatch(command_hash or "")
            or immutable_command_hash(persisted) != command_hash
        ):
            raise DispatchConflict("persisted command hash changed")

        # A queued command is an approved immutable snapshot. Do not rebuild it
        # from mutable live inventory here: inventory can legitimately change
        # while a command waits or executes, and an expired lease must redeliver
        # the exact same command. Bind the snapshot back to immutable Operation
        # and Operation Target fields instead.
        operation = self._operation(operation_name, lock=False)
        if operation is None:
            raise DispatchConflict("unknown operation")
        try:
            payload = json.loads(operation["payload_json"])
        except (TypeError, json.JSONDecodeError):
            raise DispatchConflict("operation payload is invalid JSON") from None
        expected = {
            "protocol_version": operation["protocol_version"],
            "operation_id": operation["operation_id"],
            "idempotency_key": operation["idempotency_key"],
            "agent_id": operation["agent_id"],
            "audience": operation["audience"],
            "bench_id": operation["bench_id"],
            "site_id": operation.get("site_id"),
            "operation": operation["operation_type"],
            "payload": payload,
            "requested_by": operation["requested_by"],
            "payload_hash": operation["payload_hash"],
        }
        if any(persisted.get(key) != value for key, value in expected.items()):
            raise DispatchConflict("persisted command no longer matches operation")
        if not isinstance(persisted.get("approval_claims"), list):
            raise DispatchConflict("persisted approval claims are invalid")

        target = self._target(operation_name)
        if (
            target["server_agent"] != operation["server_agent"]
            or target["bench"] != operation["bench"]
            or (target.get("managed_site") or None) != (operation.get("managed_site") or None)
            or target["operation_type_snapshot"] != operation["operation_type"]
            or target["payload_hash_snapshot"] != operation["payload_hash"]
        ):
            raise DispatchConflict("operation target snapshot changed")
        inventory_revision = target.get("inventory_revision_snapshot") or None
        if inventory_revision is not None and not _SHA256.fullmatch(inventory_revision):
            raise DispatchConflict("operation inventory snapshot is invalid")
        if persisted["approval_claims"] != self._approval_claims(operation, target):
            raise DispatchConflict("persisted approval claims changed")

        envelope = dict(persisted)
        current = _utc(now)
        envelope["issued_at"] = timestamp(current)
        envelope["expires_at"] = timestamp(
            current + timedelta(seconds=self.command_lifetime_seconds)
        )
        return envelope
