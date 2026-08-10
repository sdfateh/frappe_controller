from __future__ import annotations

import hashlib
import json
import math
import uuid

import frappe
from frappe.model.document import Document

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields,
    legal_transition,
    prevent_delete,
    require_json,
    require_sha256,
)

_TERMINAL = frozenset(
    {"succeeded", "partial", "failed", "cancelled", "needs_intervention", "rejected"}
)
_TRANSITIONS = {
    "draft": frozenset({"previewed", "cancelled"}),
    "previewed": frozenset({"awaiting_approval", "cancelled"}),
    "awaiting_approval": frozenset({"approved", "rejected", "cancelled"}),
    "approved": frozenset({"canary", "running", "paused", "cancelling", "cancelled", "needs_intervention"}),
    "canary": frozenset({"running", "paused", "cancelling", *_TERMINAL}),
    "running": frozenset({"paused", "cancelling", *_TERMINAL}),
    "paused": frozenset({"canary", "running", "cancelling", "cancelled"}),
    "cancelling": frozenset({"cancelled", "partial", "needs_intervention"}),
    **{state: frozenset() for state in _TERMINAL},
}
_IMMUTABLE = (
    "bulk_operation_id",
    "retry_of",
    "idempotency_key",
    "operation_type",
    "payload_json",
    "payload_hash",
    "selector_json",
    "selector_hash",
    "snapshot_hash",
    "dry_run_hash",
    "environment_counts_json",
    "requested_by",
    "approval_policy",
    "required_approvals",
    "maximum_targets",
    "canary_size",
    "global_concurrency",
    "per_agent_concurrency",
    "per_bench_concurrency",
    "maximum_failures",
    "maximum_failure_ratio",
    "total_targets",
    "requested_at",
    "previewed_at",
)
_COUNT_FIELDS = (
    "planned_count",
    "queued_count",
    "running_count",
    "succeeded_count",
    "failed_count",
    "cancelled_count",
    "needs_intervention_count",
)


def _service_only(document: Document) -> None:
    if not document.flags.get("controller_service"):
        frappe.throw(
            "Bulk Operation creation is restricted to the controller service",
            frappe.PermissionError,
        )


class BulkOperation(Document):
    def before_insert(self):
        _service_only(self)
        if not self.requested_by:
            self.requested_by = frappe.session.user
        elif frappe.session.user != "Administrator" and self.requested_by != frappe.session.user:
            frappe.throw("requested_by must be the authenticated user", frappe.PermissionError)

    def validate(self):
        try:
            operation_id = str(uuid.UUID(self.bulk_operation_id))
        except (ValueError, TypeError, AttributeError):
            frappe.throw("Bulk Operation ID must be a canonical UUID", frappe.ValidationError)
        if operation_id != str(self.bulk_operation_id).lower():
            frappe.throw("Bulk Operation ID must be a canonical UUID", frappe.ValidationError)
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.endswith(operation_id):
            frappe.throw("Bulk idempotency key must bind the operation UUID", frappe.ValidationError)
        if self.retry_of:
            source = frappe.db.get_value(
                "Bulk Operation", self.retry_of,
                ["operation_type", "requested_by", "state"], as_dict=True,
            )
            if (
                not source or source.operation_type != self.operation_type
                or (
                    self.is_new()
                    and source.state not in {
                        "partial", "failed", "paused", "needs_intervention"
                    }
                )
            ):
                frappe.throw("Bulk retry parent lineage is invalid", frappe.ValidationError)

        payload = require_json(self.payload_json, "payload_json")
        selector = require_json(self.selector_json, "selector_json")
        environments = require_json(self.environment_counts_json, "environment_counts_json", dict)
        for field in ("payload_hash", "selector_hash", "snapshot_hash", "dry_run_hash"):
            require_sha256(self.get(field), field)
        encoded_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
        if hashlib.sha256(encoded_payload.encode("utf-8")).hexdigest() != self.payload_hash:
            frappe.throw("Payload hash does not match payload JSON", frappe.ValidationError)
        encoded_selector = json.dumps(
            selector, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        selector_hash = hashlib.sha256(encoded_selector.encode("utf-8")).hexdigest()
        if selector_hash != self.selector_hash:
            frappe.throw("Selector hash does not match selector JSON", frappe.ValidationError)

        if set(environments) - {"development", "staging", "production"}:
            frappe.throw("Environment counts contain an unknown environment", frappe.ValidationError)
        if any(type(value) is not int or value < 0 for value in environments.values()):
            frappe.throw("Environment counts must be non-negative integers", frappe.ValidationError)

        integer_bounds = {
            "maximum_targets": (1, 1000),
            "canary_size": (1, 100),
            "global_concurrency": (1, 256),
            "per_agent_concurrency": (1, 64),
            "per_bench_concurrency": (1, 32),
            "maximum_failures": (0, 1000),
        }
        for field, (minimum, maximum) in integer_bounds.items():
            value = self.get(field)
            if type(value) is not int or not minimum <= value <= maximum:
                frappe.throw(f"{field} is outside the safe bound", frappe.ValidationError)
        if not 1 <= self.total_targets <= self.maximum_targets:
            frappe.throw("Bulk target count exceeds policy", frappe.ValidationError)
        if sum(environments.values()) != self.total_targets:
            frappe.throw("Environment counts must equal total targets", frappe.ValidationError)
        if self.canary_size > self.total_targets:
            frappe.throw("Canary size exceeds total targets", frappe.ValidationError)
        if self.per_agent_concurrency > self.global_concurrency:
            frappe.throw("Agent concurrency exceeds global concurrency", frappe.ValidationError)
        if self.per_bench_concurrency > self.per_agent_concurrency:
            frappe.throw("Bench concurrency exceeds agent concurrency", frappe.ValidationError)
        if not isinstance(self.maximum_failure_ratio, (int, float)) or isinstance(
            self.maximum_failure_ratio, bool
        ) or not math.isfinite(float(self.maximum_failure_ratio)) or not 0 <= float(
            self.maximum_failure_ratio
        ) <= 1:
            frappe.throw("Maximum failure ratio is invalid", frappe.ValidationError)

        counts = [self.get(field) for field in _COUNT_FIELDS]
        if any(type(value) is not int or value < 0 for value in counts):
            frappe.throw("Bulk aggregate counts must be non-negative integers", frappe.ValidationError)
        if sum(counts) != self.total_targets:
            frappe.throw("Bulk aggregate counts must equal total targets", frappe.ValidationError)
        if type(self.required_approvals) is not int or self.required_approvals < 1:
            frappe.throw("Bulk operations require approval", frappe.PermissionError)
        if type(self.approval_count) is not int or not 0 <= self.approval_count <= self.required_approvals:
            frappe.throw("Bulk approval count is invalid", frappe.ValidationError)
        if self.approval_status == "approved" and self.approval_count < self.required_approvals:
            frappe.throw("Bulk approval threshold is not satisfied", frappe.PermissionError)
        if self.pause_requested and not (self.pause_requested_by and self.pause_requested_at):
            frappe.throw("Bulk pause attribution is incomplete", frappe.ValidationError)
        if self.cancel_requested and not (self.cancel_requested_by and self.cancel_requested_at):
            frappe.throw("Bulk cancellation attribution is incomplete", frappe.ValidationError)
        previous_document = self.get_doc_before_save()
        if previous_document and previous_document.cancel_requested:
            if (
                not self.cancel_requested
                or previous_document.cancel_requested_by != self.cancel_requested_by
                or previous_document.cancel_requested_at != self.cancel_requested_at
            ):
                frappe.throw("Bulk cancellation request is immutable", frappe.ValidationError)
        policy = frappe.db.get_value(
            "Approval Policy",
            self.approval_policy,
            [
                "enabled", "environment", "operation_pattern", "minimum_approvals",
                "require_backup", "bulk_threshold", "maximum_targets",
            ],
            as_dict=True,
        )
        operation_allowed = bool(policy) and (
            policy.operation_pattern == self.operation_type
            or (
                isinstance(policy.operation_pattern, str)
                and policy.operation_pattern.endswith("*")
                and self.operation_type.startswith(policy.operation_pattern[:-1])
            )
        )
        environment_allowed = bool(policy) and (
            policy.environment == "any"
            or all(
                environment == policy.environment or count == 0
                for environment, count in environments.items()
            )
        )
        if (
            not policy
            or not policy.enabled
            or not operation_allowed
            or not environment_allowed
            or int(policy.minimum_approvals) != self.required_approvals
            or self.total_targets < int(policy.bulk_threshold)
            or self.total_targets > int(policy.maximum_targets)
            or self.maximum_targets > int(policy.maximum_targets)
            or (
                self.operation_type in {"data.update", "data.update.break_glass"}
                and bool(policy.require_backup)
            )
        ):
            frappe.throw("Bulk approval policy does not authorize this snapshot", frappe.PermissionError)

        immutable_fields(self, _IMMUTABLE)
        legal_transition(self, _TRANSITIONS)

    def on_trash(self):
        prevent_delete("Bulk Operation")
