from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from frappe_controller.frappe_controller.doctype._invariants import (
    immutable_fields,
    legal_transition,
    prevent_delete,
    require_json,
    require_link_value,
    require_sha256,
)

_TERMINAL = frozenset({
    "succeeded", "failed", "cancelled", "timed_out", "dead_letter",
    "needs_intervention", "rejected",
})
_TRANSITIONS = {
    "planned": frozenset({"queued", "cancelled"}),
    "queued": frozenset({"running", *_TERMINAL}),
    "running": frozenset({"queued", *_TERMINAL}),
    **{state: frozenset() for state in _TERMINAL},
}
_IMMUTABLE = (
    "bulk_operation",
    "target_key",
    "ordinal",
    "wave",
    "attempt",
    "server_agent",
    "bench",
    "managed_site",
    "agent_id_snapshot",
    "bench_id_snapshot",
    "site_id_snapshot",
    "site_domain_snapshot",
    "environment_snapshot",
    "inventory_revision",
    "preview_operation",
    "preview_result_hash",
    "retry_source",
    "retry_of",
)


class BulkOperationTarget(Document):
    def before_insert(self):
        if not self.flags.get("controller_service"):
            frappe.throw(
                "Bulk Operation Target creation is restricted to the controller service",
                frappe.PermissionError,
            )

    def validate(self):
        if type(self.ordinal) is not int or self.ordinal < 0:
            frappe.throw("Bulk target ordinal is invalid", frappe.ValidationError)
        if type(self.wave) is not int or self.wave < 0:
            frappe.throw("Bulk target wave is invalid", frappe.ValidationError)
        if type(self.attempt) is not int or self.attempt < 0:
            frappe.throw("Bulk target attempt is invalid", frappe.ValidationError)
        for field in (
            "agent_id_snapshot", "bench_id_snapshot", "site_id_snapshot",
            "site_domain_snapshot",
        ):
            if not isinstance(self.get(field), str) or not self.get(field):
                frappe.throw(f"{field} is invalid", frappe.ValidationError)
        require_sha256(self.inventory_revision, "inventory_revision")
        if bool(self.preview_operation) != bool(self.preview_result_hash):
            frappe.throw("Bulk target preview lineage is incomplete", frappe.ValidationError)
        if self.preview_result_hash:
            require_sha256(self.preview_result_hash, "preview_result_hash")
        expected_target_key = "|".join(
            f"{len(value.encode('utf-8'))}:{value}"
            for value in (
                self.agent_id_snapshot,
                self.bench_id_snapshot,
                self.site_id_snapshot,
            )
        )
        if self.target_key != expected_target_key:
            frappe.throw("Bulk target key does not match its snapshot", frappe.ValidationError)
        parent = frappe.db.get_value(
            "Bulk Operation",
            self.bulk_operation,
            [
                "total_targets", "canary_size", "operation_type", "payload_json",
                "requested_by", "cancel_requested_by",
            ],
            as_dict=True,
        )
        if not parent or self.ordinal >= int(parent.total_targets):
            frappe.throw("Bulk target ordinal exceeds its parent snapshot", frappe.ValidationError)
        expected_wave = 0 if self.ordinal < int(parent.canary_size) else 1
        if self.wave != expected_wave:
            frappe.throw("Bulk target wave differs from canary policy", frappe.ValidationError)
        require_link_value("Bench", self.bench, "server_agent", self.server_agent)
        require_link_value("Managed Site", self.managed_site, "server_agent", self.server_agent)
        require_link_value("Managed Site", self.managed_site, "bench", self.bench)

        agent = frappe.db.get_value("Server Agent", self.server_agent, "agent_id")
        bench = frappe.db.get_value("Bench", self.bench, "bench_id")
        site = frappe.db.get_value(
            "Managed Site",
            self.managed_site,
            ["site_id", "domain", "environment"],
            as_dict=True,
        )
        if (
            agent != self.agent_id_snapshot
            or bench != self.bench_id_snapshot
            or not site
            or site.site_id != self.site_id_snapshot
            or site.domain != self.site_domain_snapshot
            or site.environment != self.environment_snapshot
        ):
            frappe.throw("Bulk target snapshot differs from inventory ownership", frappe.ValidationError)

        if parent.operation_type in {"data.update", "data.update.break_glass"} and not self.preview_operation:
            frappe.throw("Bulk data update requires a successful preview", frappe.PermissionError)
        if self.preview_operation and parent.operation_type not in {
            "data.update", "data.update.break_glass"
        }:
            frappe.throw("Bulk preview lineage is not supported for this operation", frappe.ValidationError)

        preview = frappe.db.get_value(
            "Operation",
            self.preview_operation,
            [
                "operation_type", "server_agent", "bench", "managed_site",
                "requested_by", "state", "payload_json", "result_hash",
            ],
            as_dict=True,
        )
        preview_target = frappe.db.get_value(
            "Operation Target", {"operation": self.preview_operation},
            ["inventory_revision_snapshot"], as_dict=True,
        )
        try:
            preview_command = json.loads(preview.payload_json) if preview else None
            preview_payload = preview_command["payload"]
            expected_apply = {
                **preview_command,
                "payload": {**preview_payload, "dry_run": False},
            }
            parent_payload = json.loads(parent.payload_json) if parent else None
        except (TypeError, KeyError, json.JSONDecodeError):
            preview = None
            preview_payload = {}
            expected_apply = None
            parent_payload = None
        if self.preview_operation and (
            not preview or not preview_target
            or preview.operation_type != parent.operation_type
            or preview.server_agent != self.server_agent
            or preview.bench != self.bench
            or preview.managed_site != self.managed_site
            or preview.requested_by != parent.requested_by
            or preview.state != "succeeded"
            or preview.result_hash != self.preview_result_hash
            or preview_target.inventory_revision_snapshot != self.inventory_revision
            or preview_payload.get("dry_run") is not True
            or expected_apply != parent_payload
        ):
            frappe.throw("Bulk target preview evidence is invalid", frappe.PermissionError)
        if self.preview_operation and frappe.db.exists(
            "Bulk Operation Target",
            {
                "preview_operation": self.preview_operation,
                "name": ["!=", self.name or ""],
            },
        ):
            frappe.throw(
                "A dry-run preview may bind only one bulk target",
                frappe.DuplicateEntryError,
            )
        if self.preview_operation and frappe.db.exists(
            "Operation",
            {
                "preview_of": self.preview_operation,
                "name": ["!=", self.child_operation or ""],
            },
        ):
            frappe.throw(
                "A dry-run preview is already bound to an apply operation",
                frappe.DuplicateEntryError,
            )

        if frappe.db.exists(
            "Bulk Operation Target",
            {
                "bulk_operation": self.bulk_operation,
                "target_key": self.target_key,
                "attempt": self.attempt,
                "name": ["!=", self.name or ""],
            },
        ):
            frappe.throw("Bulk target attempt already exists", frappe.DuplicateEntryError)
        if frappe.db.exists(
            "Bulk Operation Target",
            {
                "bulk_operation": self.bulk_operation,
                "ordinal": self.ordinal,
                "attempt": self.attempt,
                "name": ["!=", self.name or ""],
            },
        ):
            frappe.throw("Bulk target ordinal attempt already exists", frappe.DuplicateEntryError)

        if self.attempt == 0 and self.retry_of:
            frappe.throw("Initial bulk target cannot have retry lineage", frappe.ValidationError)
        if self.retry_source:
            source_parent = frappe.db.get_value(
                "Bulk Operation", self.bulk_operation, "retry_of"
            )
            source = frappe.db.get_value(
                "Bulk Operation Target", self.retry_source,
                [
                    "bulk_operation", "target_key", "server_agent", "bench",
                    "managed_site", "agent_id_snapshot", "bench_id_snapshot",
                    "site_id_snapshot", "site_domain_snapshot", "environment_snapshot",
                    "state", "child_operation",
                ],
                as_dict=True,
            )
            if (
                not source or source.bulk_operation != source_parent
                or source.target_key != self.target_key
                or source.state not in {
                    "failed", "timed_out", "dead_letter", "needs_intervention", "rejected"
                }
                or not source.child_operation
            ):
                frappe.throw("Bulk retry source is invalid", frappe.ValidationError)
            stable = (
                "server_agent", "bench", "managed_site", "agent_id_snapshot",
                "bench_id_snapshot", "site_id_snapshot", "site_domain_snapshot",
                "environment_snapshot",
            )
            if any(source.get(field) != self.get(field) for field in stable):
                frappe.throw("Bulk retry changed stable target ownership", frappe.ValidationError)
        elif frappe.db.get_value("Bulk Operation", self.bulk_operation, "retry_of"):
            frappe.throw("Every retry parent target requires source lineage", frappe.ValidationError)
        if self.attempt > 0:
            previous = frappe.db.get_value(
                "Bulk Operation Target",
                self.retry_of,
                [
                    "bulk_operation", "target_key", "ordinal", "wave", "attempt",
                    "server_agent", "bench", "managed_site", "agent_id_snapshot",
                    "bench_id_snapshot", "site_id_snapshot", "site_domain_snapshot",
                    "environment_snapshot", "inventory_revision", "preview_operation",
                    "preview_result_hash", "child_operation", "state",
                ],
                as_dict=True,
            )
            if (
                not previous
                or previous.bulk_operation != self.bulk_operation
                or previous.target_key != self.target_key
                or int(previous.attempt) + 1 != self.attempt
                or not previous.child_operation
                or previous.state not in {
                    "failed", "timed_out", "dead_letter", "needs_intervention", "rejected"
                }
            ):
                frappe.throw("Bulk retry lineage is invalid", frappe.ValidationError)
            immutable_snapshot = (
                "ordinal", "wave", "server_agent", "bench", "managed_site",
                "agent_id_snapshot", "bench_id_snapshot", "site_id_snapshot",
                "site_domain_snapshot", "environment_snapshot", "inventory_revision",
                "preview_operation", "preview_result_hash",
            )
            if any(previous.get(field) != self.get(field) for field in immutable_snapshot):
                frappe.throw("Bulk retry changed the immutable target snapshot", frappe.ValidationError)

        if self.child_operation:
            child = frappe.db.get_value(
                "Operation",
                self.child_operation,
                ["bulk_parent", "bulk_target", "retry_of", "server_agent", "bench", "managed_site"],
                as_dict=True,
            )
            expected_retry = None
            lineage_target = self.retry_of or self.retry_source
            if lineage_target:
                expected_retry = frappe.db.get_value(
                    "Bulk Operation Target", lineage_target, "child_operation"
                )
            if (
                not child
                or child.bulk_parent != self.bulk_operation
                or child.bulk_target != self.name
                or child.retry_of != expected_retry
                or child.server_agent != self.server_agent
                or child.bench != self.bench
                or child.managed_site != self.managed_site
            ):
                frappe.throw("Bulk target child operation linkage is invalid", frappe.ValidationError)
        if self.cancellation_operation:
            cancellation = frappe.db.get_value(
                "Operation", self.cancellation_operation,
                [
                    "operation_type", "server_agent", "bench", "managed_site",
                    "requested_by", "payload_json",
                ],
                as_dict=True,
            )
            try:
                cancellation_payload = json.loads(cancellation.payload_json) if cancellation else None
            except (TypeError, json.JSONDecodeError):
                cancellation_payload = None
            if (
                not cancellation
                or cancellation.operation_type != "operation.cancel"
                or cancellation.server_agent != self.server_agent
                or cancellation.bench != self.bench
                or cancellation.managed_site != self.managed_site
                or cancellation.requested_by != parent.cancel_requested_by
                or cancellation_payload != {"target_operation_id": self.child_operation}
            ):
                frappe.throw("Bulk target cancellation linkage is invalid", frappe.ValidationError)
        previous_document = self.get_doc_before_save()
        if previous_document and previous_document.child_operation and (
            previous_document.child_operation != self.child_operation
        ):
            frappe.throw("child_operation is immutable once linked", frappe.ValidationError)
        if previous_document and previous_document.cancellation_operation and (
            previous_document.cancellation_operation != self.cancellation_operation
        ):
            frappe.throw("cancellation_operation is immutable once linked", frappe.ValidationError)
        if self.result_json:
            require_json(self.result_json, "result_json")

        immutable_fields(self, _IMMUTABLE)
        legal_transition(self, _TRANSITIONS)

    def on_trash(self):
        prevent_delete("Bulk Operation Target")
