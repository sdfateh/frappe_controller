"""Fail-closed deployment-owned feature gates for gradual controller rollout."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024
ENVIRONMENTS = ("development", "staging", "production")
FEATURES = (
    "inventory",
    "backup_and_verify",
    "routine_operations",
    "restore_and_reinstall",
    "quarantine_delete",
    "data_update_preview",
    "data_update_apply",
    "bulk_operations",
    "agent_upgrades",
)
_TOP_FIELDS = frozenset({"schema_version", "master_enabled", "features"})
_ENVIRONMENT_SET = frozenset(ENVIRONMENTS)
_OPERATION_FEATURE = {
    "site.backup": "backup_and_verify",
    "site.verify": "backup_and_verify",
    "site.scheduler.enable": "routine_operations",
    "site.scheduler.disable": "routine_operations",
    "site.maintenance.enable": "routine_operations",
    "site.maintenance.disable": "routine_operations",
    "site.config.update": "routine_operations",
    "site.config.set": "routine_operations",
    "site.create": "restore_and_reinstall",
    "site.create_blank": "restore_and_reinstall",
    "site.create_from_backup": "restore_and_reinstall",
    "site.restore": "restore_and_reinstall",
    "site.reinstall": "restore_and_reinstall",
    "site.migrate": "restore_and_reinstall",
    "site.delete": "quarantine_delete",
}


class FeatureConfigError(ValueError):
    """The deployment feature file is unavailable or unsafe."""


def _strict_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in items:
        if key in value:
            raise FeatureConfigError("feature configuration contains duplicate fields")
        value[key] = child
    return value


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    master_enabled: bool
    features: Mapping[str, frozenset[str]]

    @classmethod
    def disabled(cls) -> "FeatureConfig":
        return cls(
            False,
            MappingProxyType({feature: frozenset() for feature in FEATURES}),
        )

    def enabled(self, feature: str, environment: str) -> bool:
        return bool(
            self.master_enabled
            and feature in self.features
            and environment in self.features[feature]
        )

    def visible(self, feature: str) -> bool:
        return bool(self.master_enabled and self.features.get(feature))

    @property
    def workspace_visible(self) -> bool:
        return bool(self.master_enabled and any(self.features.values()))

    def public_value(self) -> dict[str, Any]:
        return {
            "master_enabled": self.master_enabled,
            "workspace_visible": self.workspace_visible,
            "features": {
                feature: [
                    environment
                    for environment in ENVIRONMENTS
                    if environment in self.features[feature]
                ]
                for feature in FEATURES
            },
        }


def load_feature_config(
    path: str | Path, *, require_root_owner: bool = True
) -> FeatureConfig:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise FeatureConfigError("feature configuration path must be absolute")
    try:
        info = candidate.lstat()
    except OSError:
        raise FeatureConfigError("feature configuration is unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise FeatureConfigError("feature configuration must be a regular file")
    if require_root_owner and info.st_uid != 0:
        raise FeatureConfigError("feature configuration must be root-owned")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise FeatureConfigError("feature configuration is group/world writable")
    if not 0 < info.st_size <= MAX_CONFIG_BYTES:
        raise FeatureConfigError("feature configuration size is invalid")
    try:
        raw = candidate.read_bytes()
        if len(raw) != info.st_size:
            raise FeatureConfigError("feature configuration changed while reading")
        document = json.loads(raw, object_pairs_hook=_strict_object)
    except FeatureConfigError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise FeatureConfigError("feature configuration is invalid JSON") from None
    if (
        not isinstance(document, dict)
        or set(document) != _TOP_FIELDS
        or document.get("schema_version") != SCHEMA_VERSION
        or type(document.get("master_enabled")) is not bool
        or not isinstance(document.get("features"), dict)
        or set(document["features"]) != set(FEATURES)
    ):
        raise FeatureConfigError("feature configuration fields or version are invalid")

    normalized: dict[str, frozenset[str]] = {}
    for feature in FEATURES:
        environments = document["features"][feature]
        if (
            not isinstance(environments, list)
            or any(not isinstance(item, str) for item in environments)
            or len(environments) != len(set(environments))
            or set(environments) - _ENVIRONMENT_SET
        ):
            raise FeatureConfigError("feature environment list is invalid")
        normalized[feature] = frozenset(environments)

    inventory = normalized["inventory"]
    if any(environments - inventory for environments in normalized.values()):
        raise FeatureConfigError("enabled features require inventory in the same environment")
    if normalized["data_update_apply"] - normalized["data_update_preview"]:
        raise FeatureConfigError("data update apply requires preview")
    if normalized["bulk_operations"] - normalized["data_update_apply"]:
        raise FeatureConfigError("bulk operations require data update apply")
    return FeatureConfig(True if document["master_enabled"] else False, MappingProxyType(normalized))


def feature_config_path(frappe_module: Any) -> Path:
    configured = frappe_module.conf.get("frappe_controller_feature_flags_path")
    if configured is not None:
        if not isinstance(configured, str) or not configured:
            raise FeatureConfigError("feature configuration path is invalid")
        return Path(configured)
    return Path(frappe_module.get_site_path("controller-feature-flags.json"))


def runtime_feature_config(frappe_module: Any | None = None) -> FeatureConfig:
    if frappe_module is None:
        import frappe as frappe_module
    try:
        in_test = bool(getattr(getattr(frappe_module, "flags", None), "in_test", False))
        return load_feature_config(
            feature_config_path(frappe_module), require_root_owner=not in_test
        )
    except FeatureConfigError:
        return FeatureConfig.disabled()


def agent_environment(frappe_module: Any, agent_id: str) -> str | None:
    if not isinstance(agent_id, str) or not agent_id:
        return None
    environment = frappe_module.db.get_value(
        "Server Agent", {"agent_id": agent_id}, "environment"
    )
    return environment if environment in _ENVIRONMENT_SET else None


def target_environment(
    frappe_module: Any, agent_id: str, managed_site: str | None
) -> str | None:
    if managed_site:
        environment = frappe_module.db.get_value(
            "Managed Site", managed_site, "environment"
        )
        return environment if environment in _ENVIRONMENT_SET else None
    return agent_environment(frappe_module, agent_id)


def operation_feature(operation_type: str, payload_json: str | None = None) -> str | None:
    if operation_type in {"data.update", "data.update.break_glass"}:
        try:
            command = json.loads(payload_json or "")
            dry_run = command["payload"]["dry_run"]
        except (TypeError, KeyError, json.JSONDecodeError):
            return None
        if type(dry_run) is not bool:
            return None
        return "data_update_preview" if dry_run else "data_update_apply"
    return _OPERATION_FEATURE.get(operation_type)


def operation_enabled(
    config: FeatureConfig,
    *,
    environment: str,
    operation_type: str,
    payload_json: str | None = None,
    bulk_parent: str | None = None,
) -> bool:
    if operation_type == "operation.cancel":
        return True
    feature = operation_feature(operation_type, payload_json)
    if feature is None or not config.enabled(feature, environment):
        return False
    return not bulk_parent or config.enabled("bulk_operations", environment)


def require_feature(
    frappe_module: Any, feature: str, environment: str | None
) -> None:
    if environment not in _ENVIRONMENT_SET or not runtime_feature_config(
        frappe_module
    ).enabled(feature, environment):
        frappe_module.throw("Controller feature is disabled", frappe_module.PermissionError)


def require_operation(
    frappe_module: Any,
    *,
    environment: str | None,
    operation_type: str,
    payload_json: str | None = None,
    bulk_parent: str | None = None,
) -> None:
    config = runtime_feature_config(frappe_module)
    if environment not in _ENVIRONMENT_SET or not operation_enabled(
        config,
        environment=environment,
        operation_type=operation_type,
        payload_json=payload_json,
        bulk_parent=bulk_parent,
    ):
        frappe_module.throw("Controller feature is disabled", frappe_module.PermissionError)


def enabled_operations_for_agent(frappe_module: Any, agent_id: str) -> tuple[str, ...]:
    environment = agent_environment(frappe_module, agent_id)
    if environment is None:
        return ("operation.cancel",)
    config = runtime_feature_config(frappe_module)
    enabled = [
        operation
        for operation, feature in _OPERATION_FEATURE.items()
        if config.enabled(feature, environment)
    ]
    if config.enabled("data_update_preview", environment) or config.enabled(
        "data_update_apply", environment
    ):
        enabled.extend(("data.update", "data.update.break_glass"))
    enabled.append("operation.cancel")
    return tuple(sorted(set(enabled)))


def command_enabled_for_agent(
    frappe_module: Any, agent_id: str, operation: Mapping[str, Any]
) -> bool:
    del agent_id
    environment = operation.get("target_environment")
    if environment is None:
        return operation.get("operation_type") == "operation.cancel"
    return operation_enabled(
        runtime_feature_config(frappe_module),
        environment=environment,
        operation_type=operation.get("operation_type"),
        payload_json=operation.get("payload_json"),
        bulk_parent=operation.get("bulk_parent"),
    )


def bulk_parent_enabled(frappe_module: Any, parent_id: str) -> bool:
    environments = frappe_module.get_all(
        "Bulk Operation Target",
        filters={"bulk_operation": parent_id},
        pluck="environment_snapshot",
        limit_page_length=1001,
    )
    config = runtime_feature_config(frappe_module)
    return bool(environments) and all(
        environment in _ENVIRONMENT_SET
        and config.enabled("bulk_operations", environment)
        for environment in set(environments)
    )


def extend_bootinfo(bootinfo: Any) -> None:
    """Expose only non-secret flag values for Desk visibility decisions."""
    import frappe

    actor = getattr(getattr(frappe, "session", None), "user", None)
    roles = set(frappe.get_roles(actor)) if actor and actor != "Guest" else set()
    if not ({"System Manager", "Controller Admin", "Operator", "Approver", "Auditor"} & roles):
        value = FeatureConfig.disabled().public_value()
    else:
        value = runtime_feature_config(frappe).public_value()
    if isinstance(bootinfo, dict):
        bootinfo["frappe_controller_features"] = value
    else:
        bootinfo.frappe_controller_features = value
    if not value["workspace_visible"]:
        for field in ("allowed_workspaces", "workspaces"):
            workspaces = (
                bootinfo.get(field) if isinstance(bootinfo, dict)
                else getattr(bootinfo, field, None)
            )
            if isinstance(workspaces, list):
                filtered = [
                    workspace for workspace in workspaces
                    if not isinstance(workspace, Mapping)
                    or workspace.get("name") != "Frappe Controller"
                    and workspace.get("label") != "Frappe Controller"
                    and workspace.get("title") != "Frappe Controller"
                ]
                if isinstance(bootinfo, dict):
                    bootinfo[field] = filtered
                else:
                    setattr(bootinfo, field, filtered)


def sync_workspace_visibility(frappe_module: Any | None = None) -> dict[str, bool]:
    """Apply file-derived visibility to the standard controller Workspace."""
    if frappe_module is None:
        import frappe as frappe_module
    config = runtime_feature_config(frappe_module)
    if not frappe_module.db.exists("Workspace", "Frappe Controller"):
        return {
            "workspace_visible": False,
            "inventory": False,
            "operations": False,
            "bulk": False,
        }

    inventory_visible = config.visible("inventory")
    operations_visible = any(
        config.visible(feature)
        for feature in (
            "backup_and_verify", "routine_operations", "restore_and_reinstall",
            "quarantine_delete", "data_update_preview", "data_update_apply",
        )
    )
    bulk_visible = config.visible("bulk_operations")
    groups = {
        "Fleet Inventory": inventory_visible,
        "Operations and Failures": operations_visible,
        "Bulk Operations": bulk_visible,
    }
    link_group = {
        "Fleet Inventory": "Fleet Inventory",
        "Server Agents": "Fleet Inventory",
        "Benches": "Fleet Inventory",
        "Managed Sites": "Fleet Inventory",
        "Operations and Failures": "Operations and Failures",
        "Operations": "Operations and Failures",
        "Operation Types": "Operations and Failures",
        "Agent Operations": "Operations and Failures",
        "Operation Failures": "Operations and Failures",
        "Bulk Operations": "Bulk Operations",
        "Bulk Targets": "Bulk Operations",
        "Bulk Progress": "Bulk Operations",
        "Bulk Failures": "Bulk Operations",
    }
    workspace = frappe_module.get_doc("Workspace", "Frappe Controller")
    workspace.is_hidden = 0 if config.workspace_visible else 1
    for link in workspace.links:
        group = link_group.get(link.label)
        link.hidden = 0 if group and groups[group] else 1

    source_path = (
        Path(__file__).resolve().parent
        / "frappe_controller" / "workspace" / "frappe_controller"
        / "frappe_controller.json"
    )
    source = json.loads(source_path.read_text(encoding="utf-8"))
    blocks = json.loads(source["content"])
    workspace.content = json.dumps(
        [
            block for block in blocks
            if block.get("type") != "card"
            or groups.get(block.get("data", {}).get("card_name"), False)
        ],
        separators=(",", ":"),
    )
    workspace.save(ignore_permissions=True)
    frappe_module.clear_cache()
    return {
        "workspace_visible": config.workspace_visible,
        "inventory": inventory_visible,
        "operations": operations_visible,
        "bulk": bulk_visible,
    }


__all__ = [
    "ENVIRONMENTS", "FEATURES", "FeatureConfig", "FeatureConfigError",
    "agent_environment", "bulk_parent_enabled", "command_enabled_for_agent",
    "enabled_operations_for_agent", "extend_bootinfo",
    "feature_config_path", "load_feature_config", "operation_enabled",
    "operation_feature", "require_feature", "require_operation",
    "runtime_feature_config", "sync_workspace_visibility", "target_environment",
]
