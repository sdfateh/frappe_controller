"""Server-controlled authoring for immutable single-target operations."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence


_ENVIRONMENTS = frozenset({"development", "staging", "production"})
_SENSITIVE = (
    "api_key", "credential", "encryption_key", "password", "private_key",
    "secret", "token",
)


class OperationAuthoringError(ValueError):
    """An operation cannot be safely authored under server policy."""


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    server_agent: str
    agent_id: str
    bench: str
    bench_id: str
    managed_site: str | None
    site_domain: str | None
    environment: str
    inventory_revision: str
    capabilities: frozenset[str]
    agent_enabled: bool = True
    site_active: bool = True

    def __post_init__(self) -> None:
        required = (self.server_agent, self.agent_id, self.bench, self.bench_id)
        if any(not isinstance(value, str) or not value for value in required):
            raise OperationAuthoringError("target identity is incomplete")
        if self.environment not in _ENVIRONMENTS:
            raise OperationAuthoringError("target environment is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.inventory_revision):
            raise OperationAuthoringError("target inventory revision is invalid")
        if not isinstance(self.capabilities, frozenset):
            raise OperationAuthoringError("target capabilities must be immutable")
        if (self.managed_site is None) != (self.site_domain is None):
            raise OperationAuthoringError("site identity must be complete or absent")


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    name: str
    environment: str
    operation_pattern: str
    minimum_approvals: int
    require_backup: bool
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name or self.environment not in {*_ENVIRONMENTS, "any"}:
            raise OperationAuthoringError("approval rule identity is invalid")
        if (
            not isinstance(self.minimum_approvals, int)
            or isinstance(self.minimum_approvals, bool)
            or self.minimum_approvals < 1
        ):
            raise OperationAuthoringError("approval rule threshold is invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]*(?:\.\*)?", self.operation_pattern):
            raise OperationAuthoringError("approval operation pattern is invalid")

    def matches(self, environment: str, operation: str) -> bool:
        if not self.enabled or self.environment not in {"any", environment}:
            return False
        if self.operation_pattern.endswith(".*"):
            return operation.startswith(self.operation_pattern[:-1])
        return operation == self.operation_pattern


@dataclass(frozen=True, slots=True)
class PayloadAuthorization:
    normalized_payload: Mapping[str, Any]
    site_required: bool
    destructive: bool
    pre_operation_backup: bool
    approval_required: bool = False
    payload_contains_site_identity: bool = True
    pre_approval_preview: bool = False


PayloadValidator = Callable[[str, Mapping[str, Any]], PayloadAuthorization]


@dataclass(frozen=True, slots=True)
class OperationRequest:
    operation_id: str
    operation_type: str
    server_agent: str
    bench: str
    managed_site: str | None
    payload: Mapping[str, Any]
    preview_of: str | None = None
    preview_result_hash: str | None = None


@dataclass(frozen=True, slots=True)
class AuthoredOperation:
    operation_id: str
    idempotency_key: str
    protocol_version: str
    server_agent: str
    agent_id: str
    bench: str
    bench_id: str
    managed_site: str | None
    site_domain: str | None
    operation_type: str
    payload_json: str
    payload_hash: str
    requested_by: str
    approval_policy: str | None
    approval_status: str
    required_approvals: int
    state: str
    inventory_revision: str
    target_key: str
    preview_of: str | None
    preview_result_hash: str | None


class OperationAuthoringRepository(Protocol):
    def resolve_target(
        self, server_agent: str, bench: str, managed_site: str | None
    ) -> TargetSnapshot: ...

    def approval_rules(self) -> Sequence[ApprovalRule]: ...

    def create(self, operation: AuthoredOperation) -> Any: ...


def _has_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(marker in normalized for marker in _SENSITIVE):
                return True
            if _has_sensitive_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_sensitive_key(item) for item in value)
    return False


def _canonical_payload(value: Mapping[str, Any]) -> tuple[str, str]:
    if _has_sensitive_key(value):
        raise OperationAuthoringError("operation payload contains a sensitive field")
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise OperationAuthoringError("operation payload is not canonical JSON") from None
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise OperationAuthoringError("operation payload exceeds its size limit")
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def select_approval_rule(
    rules: Sequence[ApprovalRule], environment: str, operation: str
) -> ApprovalRule | None:
    matching = [rule for rule in rules if rule.matches(environment, operation)]
    if not matching:
        return None
    matching.sort(
        key=lambda rule: (
            rule.environment == environment,
            not rule.operation_pattern.endswith(".*"),
            len(rule.operation_pattern),
        ),
        reverse=True,
    )
    best = matching[0]
    best_rank = (
        best.environment == environment,
        not best.operation_pattern.endswith(".*"),
        len(best.operation_pattern),
    )
    if sum(
        (
            rule.environment == environment,
            not rule.operation_pattern.endswith(".*"),
            len(rule.operation_pattern),
        ) == best_rank
        for rule in matching
    ) != 1:
        raise OperationAuthoringError("approval policy selection is ambiguous")
    return best


class OperationAuthoringService:
    def __init__(
        self,
        repository: OperationAuthoringRepository,
        payload_validator: PayloadValidator,
    ) -> None:
        self.repository = repository
        self.payload_validator = payload_validator

    def author(self, request: OperationRequest, *, actor: str) -> AuthoredOperation:
        if not actor or not isinstance(actor, str):
            raise OperationAuthoringError("authenticated actor is required")
        try:
            operation_uuid = str(uuid.UUID(request.operation_id))
        except (ValueError, TypeError, AttributeError):
            raise OperationAuthoringError("operation id must be a UUID") from None
        if operation_uuid != request.operation_id.lower():
            raise OperationAuthoringError("operation id must be canonical")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", request.operation_type):
            raise OperationAuthoringError("operation type is invalid")

        target = self.repository.resolve_target(
            request.server_agent, request.bench, request.managed_site
        )
        if not target.agent_enabled or (target.managed_site and not target.site_active):
            raise OperationAuthoringError("operation target is inactive")
        if request.operation_type not in target.capabilities:
            raise OperationAuthoringError("operation is not supported by the target")
        authorization = self.payload_validator(request.operation_type, request.payload)
        if authorization.pre_approval_preview and (
            authorization.destructive or authorization.approval_required
        ):
            raise OperationAuthoringError("pre-approval preview contract is invalid")
        if authorization.site_required != (target.managed_site is not None):
            raise OperationAuthoringError("operation target kind does not match payload contract")
        payload_domain = authorization.normalized_payload.get("domain")
        if (
            authorization.payload_contains_site_identity
            and target.site_domain is not None
            and payload_domain != target.site_domain
        ):
            raise OperationAuthoringError("payload site does not match target snapshot")
        if authorization.pre_operation_backup is False and authorization.destructive:
            raise OperationAuthoringError("destructive operation requires a pre-operation backup")

        policy = None if authorization.pre_approval_preview else select_approval_rule(
            self.repository.approval_rules(), target.environment, request.operation_type
        )
        if (authorization.destructive or authorization.approval_required) and policy is None:
            raise OperationAuthoringError("operation requires an approval policy")
        if target.environment == "production" and policy is None and not authorization.pre_approval_preview:
            raise OperationAuthoringError("production operation has no approval policy")
        if policy is not None and policy.require_backup and not authorization.pre_operation_backup:
            raise OperationAuthoringError("approval policy requires a pre-operation backup")

        payload_json, payload_hash = _canonical_payload(authorization.normalized_payload)
        if (request.preview_of is None) != (request.preview_result_hash is None):
            raise OperationAuthoringError("preview lineage is incomplete")
        if request.preview_of is not None and request.operation_type not in {
            "data.update", "data.update.break_glass"
        }:
            raise OperationAuthoringError("preview lineage is only valid for data updates")
        preview_of = None
        if request.preview_of is not None:
            try:
                preview_of = str(uuid.UUID(request.preview_of))
            except (ValueError, TypeError, AttributeError):
                raise OperationAuthoringError("preview operation id must be a UUID") from None
            if preview_of != request.preview_of.lower() or not re.fullmatch(
                r"[0-9a-f]{64}", request.preview_result_hash or ""
            ):
                raise OperationAuthoringError("preview lineage is invalid")
        required = policy.minimum_approvals if policy else 0
        target_identity = json.dumps(
            [
                operation_uuid,
                target.agent_id,
                target.bench_id,
                target.managed_site,
                target.inventory_revision,
            ],
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        authored = AuthoredOperation(
            operation_id=operation_uuid,
            idempotency_key=f"controller:{operation_uuid}",
            protocol_version="1.0",
            server_agent=target.server_agent,
            agent_id=target.agent_id,
            bench=target.bench,
            bench_id=target.bench_id,
            managed_site=target.managed_site,
            site_domain=target.site_domain,
            operation_type=request.operation_type,
            payload_json=payload_json,
            payload_hash=payload_hash,
            requested_by=actor,
            approval_policy=policy.name if policy else None,
            approval_status="pending" if required else "not_required",
            required_approvals=required,
            state="awaiting_approval",
            inventory_revision=target.inventory_revision,
            target_key=hashlib.sha256(target_identity).hexdigest(),
            preview_of=preview_of,
            preview_result_hash=request.preview_result_hash,
        )
        self.repository.create(authored)
        return authored


__all__ = [
    "ApprovalRule", "AuthoredOperation", "OperationAuthoringError",
    "OperationAuthoringRepository", "OperationAuthoringService", "OperationRequest",
    "PayloadAuthorization", "TargetSnapshot", "select_approval_rule",
]
