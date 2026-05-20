import contextlib
import importlib.util
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from infra.config.models import InfraSettings


class _PluginMetadata(Protocol):
    name: str
    default_enabled: bool
    dependencies: list[str]
    optional_dependencies: list[str]
    provides: list[str]
    service_name_config: str | None


class _ConfigurablePlugin(Protocol):
    metadata: _PluginMetadata
    config_model: type[BaseModel] | None


class _PluginConfigValidator(Protocol):
    def validate_config(self, config: Any) -> None:
        raise NotImplementedError


class _ServiceReferenceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_service: str | None = None
    required_when: str | None = None
    required_when_config: dict[str, Any] = Field(default_factory=dict)
    required_unless_config: dict[str, Any] = Field(default_factory=dict)
    optional: bool = False
    description: str = ""


class _ManifestHintsSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service_references: dict[str, _ServiceReferenceSpec] = Field(default_factory=dict)


@dataclass(frozen=True)
class InfraConfigValidationIssue:
    plugin: str
    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


def validate_infra_settings(
    settings: InfraSettings,
    plugins: Iterable[Any],
) -> list[InfraConfigValidationIssue]:
    plugin_by_name: dict[str, _ConfigurablePlugin] = {
        plugin.metadata.name: plugin for plugin in plugins
    }
    issues: list[InfraConfigValidationIssue] = []

    for plugin_name in sorted(set(settings.infra.plugins) - set(plugin_by_name)):
        issues.append(
            InfraConfigValidationIssue(
                plugin=plugin_name,
                code="unknown_plugin",
                message=f"unknown configured plugin: {plugin_name}",
            )
        )

    active_plugins = {
        plugin_name
        for plugin_name, plugin in plugin_by_name.items()
        if _plugin_is_active(settings, plugin_name, plugin)
    }
    for plugin_name in sorted(active_plugins):
        plugin = plugin_by_name[plugin_name]
        _validate_dependencies(plugin_name, plugin, active_plugins, plugin_by_name, issues)
        _validate_optional_dependencies(plugin_name, plugin, issues)

    for plugin_name in sorted(set(settings.infra.plugins) & set(plugin_by_name)):
        plugin = plugin_by_name[plugin_name]
        plugin_settings = settings.get_plugin(plugin_name)
        if plugin_settings.enabled is False:
            continue
        if plugin_settings.enabled is None and not plugin.metadata.default_enabled:
            continue
        if plugin.config_model is None:
            continue
        try:
            config = plugin.config_model.model_validate(plugin_settings.config)
            if _has_config_validator(plugin):
                plugin.validate_config(config)
        except (ValidationError, ValueError) as exc:
            details: dict[str, object] = {}
            if isinstance(exc, ValidationError):
                details["errors"] = exc.errors(include_url=False)
            issues.append(
                InfraConfigValidationIssue(
                    plugin=plugin_name,
                    code="invalid_config",
                    message=str(exc),
                    details=details,
                )
            )

    _validate_service_references(settings, active_plugins, plugin_by_name, issues)

    return issues


def _validate_service_references(
    settings: InfraSettings,
    active_plugins: set[str],
    plugin_by_name: dict[str, _ConfigurablePlugin],
    issues: list[InfraConfigValidationIssue],
) -> None:
    active_services = _active_service_names(settings, active_plugins, plugin_by_name)
    for plugin_name in sorted(active_plugins):
        plugin = plugin_by_name[plugin_name]
        config = _validated_config(settings, plugin_by_name, plugin_name)
        if config is None:
            continue
        raw_config = settings.get_plugin(plugin_name).config
        try:
            hints = _manifest_hints(plugin)
        except ValidationError as exc:
            issues.append(
                InfraConfigValidationIssue(
                    plugin=plugin_name,
                    code="invalid_manifest",
                    message=str(exc),
                    details={"errors": exc.errors(include_url=False)},
                )
            )
            continue
        for field_name, reference in hints.service_references.items():
            service = _config_path_value(config, field_name)
            if not isinstance(service, str) or not service.strip():
                continue
            if not _service_reference_needs_active_service(
                reference,
                field_name=field_name,
                raw_config=raw_config,
                config=config,
            ):
                continue
            normalized = service.strip()
            if normalized not in active_services:
                _missing_service_reference(
                    issues,
                    plugin=plugin_name,
                    field=field_name,
                    service=normalized,
                    active_services=active_services,
                )


def _active_service_names(
    settings: InfraSettings,
    active_plugins: set[str],
    plugin_by_name: dict[str, _ConfigurablePlugin],
) -> set[str]:
    services: set[str] = set()
    for plugin_name in active_plugins:
        plugin = plugin_by_name[plugin_name]
        metadata = plugin.metadata
        services.update(service for service in getattr(metadata, "provides", []) if service)
        field_name = getattr(metadata, "service_name_config", None)
        if field_name is None:
            continue
        configured_service = settings.get_plugin(plugin_name).config.get(field_name)
        if isinstance(configured_service, str) and configured_service.strip():
            services.add(configured_service.strip())
    return services


def _validated_config(
    settings: InfraSettings,
    plugin_by_name: dict[str, _ConfigurablePlugin],
    plugin_name: str,
) -> BaseModel | None:
    plugin = plugin_by_name.get(plugin_name)
    if plugin is None or plugin.config_model is None:
        return None
    plugin_settings = settings.get_plugin(plugin_name)
    if not _plugin_is_active(settings, plugin_name, plugin):
        return None
    with contextlib.suppress(ValidationError, ValueError):
        return plugin.config_model.model_validate(plugin_settings.config)
    return None


def _manifest_hints(plugin: _ConfigurablePlugin) -> _ManifestHintsSpec:
    return _ManifestHintsSpec.model_validate(getattr(plugin, "manifest_hints", {}))


def _service_reference_needs_active_service(
    reference: _ServiceReferenceSpec,
    *,
    field_name: str,
    raw_config: dict[str, object],
    config: BaseModel,
) -> bool:
    if reference.optional:
        return False
    if _raw_config_has_path(raw_config, field_name):
        return True
    if reference.required_when_config and _config_matches(
        config,
        reference.required_when_config,
    ):
        return True
    if reference.required_unless_config and not _config_matches(
        config,
        reference.required_unless_config,
    ):
        return True
    return not reference.required_when_config and not reference.required_unless_config


def _config_matches(config: BaseModel, expected_values: dict[str, Any]) -> bool:
    return all(
        _config_path_value(config, field_name) == expected
        for field_name, expected in expected_values.items()
    )


def _config_path_value(config: BaseModel | dict[str, Any], field_name: str) -> Any:
    value: Any = config
    for part in field_name.split("."):
        if isinstance(value, BaseModel):
            value = getattr(value, part, None)
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _raw_config_has_path(raw_config: dict[str, object], field_name: str) -> bool:
    value: object = raw_config
    for part in field_name.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True


def _missing_service_reference(
    issues: list[InfraConfigValidationIssue],
    *,
    plugin: str,
    field: str,
    service: str,
    active_services: set[str],
) -> None:
    issues.append(
        InfraConfigValidationIssue(
            plugin=plugin,
            code="missing_service_reference",
            message=f"{plugin}.{field} references inactive service: {service}",
            details={
                "field": field,
                "service": service,
                "active_services": sorted(active_services),
            },
        )
    )


def _plugin_is_active(
    settings: InfraSettings,
    plugin_name: str,
    plugin: _ConfigurablePlugin,
) -> bool:
    plugin_settings = settings.get_plugin(plugin_name)
    if plugin_settings.enabled is False:
        return False
    if plugin_settings.enabled is True:
        return True
    return plugin.metadata.default_enabled


def _validate_dependencies(
    plugin_name: str,
    plugin: _ConfigurablePlugin,
    active_plugins: set[str],
    plugin_by_name: dict[str, _ConfigurablePlugin],
    issues: list[InfraConfigValidationIssue],
) -> None:
    unknown = [
        dependency
        for dependency in plugin.metadata.dependencies
        if dependency not in plugin_by_name
    ]
    if unknown:
        issues.append(
            InfraConfigValidationIssue(
                plugin=plugin_name,
                code="unknown_dependency",
                message="unknown required dependency: " + ", ".join(unknown),
                details={"missing_dependencies": unknown},
            )
        )

    inactive = [
        dependency
        for dependency in plugin.metadata.dependencies
        if dependency in plugin_by_name and dependency not in active_plugins
    ]
    if inactive:
        issues.append(
            InfraConfigValidationIssue(
                plugin=plugin_name,
                code="inactive_dependency",
                message="inactive required dependency: " + ", ".join(inactive),
                details={"inactive_dependencies": inactive},
            )
        )


def _validate_optional_dependencies(
    plugin_name: str,
    plugin: _ConfigurablePlugin,
    issues: list[InfraConfigValidationIssue],
) -> None:
    missing = [
        dependency
        for dependency in plugin.metadata.optional_dependencies
        if importlib.util.find_spec(dependency.replace("-", "_")) is None
    ]
    if missing:
        issues.append(
            InfraConfigValidationIssue(
                plugin=plugin_name,
                code="missing_optional_dependency",
                message="missing optional dependency: " + ", ".join(missing),
                details={"missing_optional_dependencies": missing},
            )
        )


def _has_config_validator(plugin: object) -> TypeGuard[_PluginConfigValidator]:
    return callable(getattr(plugin, "validate_config", None))
