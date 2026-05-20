from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import InfraPlugin, PluginMetadata, resolve_plugin_manifest_hints
from infra.plugins.manager import PluginDependencyError, PluginManager


@dataclass(frozen=True)
class PluginConformanceIssue:
    plugin: str
    code: str
    message: str
    severity: str = "error"


@dataclass(frozen=True)
class PluginConformanceResult:
    name: str
    valid: bool
    issues: tuple[PluginConformanceIssue, ...]

    def asdict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "valid": self.valid,
            "issues": [asdict(issue) for issue in self.issues],
        }


def check_plugins_conformance(
    plugins: Iterable[InfraPlugin],
    *,
    settings: InfraSettings | None = None,
    lifecycle: bool = False,
) -> list[PluginConformanceResult]:
    plugin_tuple = tuple(plugins)
    resolved_settings = settings or _settings_enabling(plugin_tuple)
    duplicate_names = _duplicate_plugin_names(plugin_tuple)
    results = [
        _check_single_plugin_conformance(
            plugin,
            settings=resolved_settings,
            duplicate_names=duplicate_names,
        )
        for plugin in plugin_tuple
    ]
    if lifecycle:
        results = _merge_lifecycle_results(
            results,
            _run_lifecycle_check(plugin_tuple, resolved_settings),
        )
    return results


def conformance_report(results: Iterable[PluginConformanceResult]) -> dict[str, object]:
    result_tuple = tuple(results)
    errors = [
        issue for result in result_tuple for issue in result.issues if issue.severity == "error"
    ]
    warnings = [
        issue for result in result_tuple for issue in result.issues if issue.severity == "warning"
    ]
    return {
        "valid": not errors,
        "summary": {
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "plugins": [result.asdict() for result in result_tuple],
    }


def _check_single_plugin_conformance(
    plugin: InfraPlugin,
    *,
    settings: InfraSettings,
    duplicate_names: set[str],
) -> PluginConformanceResult:
    issues: list[PluginConformanceIssue] = []
    name = _plugin_name(plugin)
    metadata = _validate_metadata(plugin, name, issues)
    if metadata is not None:
        name = metadata.name
        if name in duplicate_names:
            _error(issues, name, "duplicate_name", f"duplicate plugin name: {name}")
        _validate_config_model(plugin, metadata, settings, issues)
        _validate_manifest_hints(plugin, metadata, issues)
    _validate_required_methods(plugin, name, issues)
    return PluginConformanceResult(
        name=name,
        valid=not any(issue.severity == "error" for issue in issues),
        issues=tuple(issues),
    )


def _validate_metadata(
    plugin: InfraPlugin,
    fallback_name: str,
    issues: list[PluginConformanceIssue],
) -> PluginMetadata | None:
    raw_metadata = getattr(plugin, "metadata", None)
    try:
        return PluginMetadata.model_validate(raw_metadata)
    except ValidationError as exc:
        _error(
            issues,
            fallback_name,
            "invalid_metadata",
            f"metadata must validate as PluginMetadata: {exc}",
        )
    except Exception as exc:
        _error(
            issues,
            fallback_name,
            "invalid_metadata",
            f"metadata must be accessible and validate as PluginMetadata: {exc}",
        )
    return None


def _validate_required_methods(
    plugin: InfraPlugin,
    name: str,
    issues: list[PluginConformanceIssue],
) -> None:
    for method_name in ("register", "startup", "shutdown", "health_check"):
        if not callable(getattr(plugin, method_name, None)):
            _error(
                issues,
                name,
                "missing_method",
                f"plugin must define callable {method_name}()",
            )


def _validate_config_model(
    plugin: InfraPlugin,
    metadata: PluginMetadata,
    settings: InfraSettings,
    issues: list[PluginConformanceIssue],
) -> None:
    config_model = getattr(plugin, "config_model", None)
    if config_model is None:
        return
    if not isinstance(config_model, type) or not issubclass(config_model, BaseModel):
        _error(
            issues,
            metadata.name,
            "invalid_config_model",
            "config_model must be None or a pydantic BaseModel subclass",
        )
        return
    try:
        config = config_model.model_validate(settings.get_plugin(metadata.name).config)
        validator = getattr(plugin, "validate_config", None)
        if callable(validator):
            validator(config)
    except (ValidationError, ValueError) as exc:
        _error(
            issues,
            metadata.name,
            "invalid_default_config",
            f"configured plugin settings do not validate: {exc}",
        )


def _validate_manifest_hints(
    plugin: InfraPlugin,
    metadata: PluginMetadata,
    issues: list[PluginConformanceIssue],
) -> None:
    try:
        hints = resolve_plugin_manifest_hints(plugin)
    except (TypeError, ValueError, ValidationError) as exc:
        _error(
            issues,
            metadata.name,
            "invalid_manifest_hints",
            f"manifest_hints must validate as PluginManifestHints: {exc}",
        )
        return

    declared_services = set(metadata.provides)
    unknown_service_keys = sorted(set(hints.service_keys) - declared_services)
    if unknown_service_keys:
        _error(
            issues,
            metadata.name,
            "undeclared_service_key",
            "service_keys must reference services declared in metadata.provides: "
            + ", ".join(unknown_service_keys),
        )


def _run_lifecycle_check(
    plugins: tuple[InfraPlugin, ...],
    settings: InfraSettings,
) -> dict[str, tuple[PluginConformanceIssue, ...]]:
    try:
        return asyncio.run(_run_lifecycle_check_async(plugins, settings))
    except RuntimeError as exc:
        return {
            _plugin_name(plugin): (
                PluginConformanceIssue(
                    _plugin_name(plugin),
                    "lifecycle_not_run",
                    f"lifecycle check could not run: {exc}",
                ),
            )
            for plugin in plugins
        }


async def _run_lifecycle_check_async(
    plugins: tuple[InfraPlugin, ...],
    settings: InfraSettings,
) -> dict[str, tuple[PluginConformanceIssue, ...]]:
    manager = PluginManager(settings=settings, plugins=list(plugins))
    try:
        await manager.startup()
        health = await manager.refresh_health()
        issues: dict[str, tuple[PluginConformanceIssue, ...]] = {}
        for name, status in health.items():
            if status.status is HealthState.UNHEALTHY:
                issues[name] = (
                    PluginConformanceIssue(
                        name,
                        "unhealthy",
                        status.message or "plugin health check returned unhealthy",
                    ),
                )
        return issues
    except PluginDependencyError as exc:
        return _lifecycle_error_for_plugins(plugins, str(exc))
    except Exception as exc:
        return _lifecycle_error_for_plugins(plugins, str(exc))
    finally:
        try:
            await manager.shutdown()
        except Exception:
            pass


def _lifecycle_error_for_plugins(
    plugins: Iterable[InfraPlugin],
    message: str,
) -> dict[str, tuple[PluginConformanceIssue, ...]]:
    return {
        _plugin_name(plugin): (
            PluginConformanceIssue(
                _plugin_name(plugin),
                "lifecycle_failed",
                message,
            ),
        )
        for plugin in plugins
    }


def _merge_lifecycle_results(
    results: list[PluginConformanceResult],
    lifecycle_issues: dict[str, tuple[PluginConformanceIssue, ...]],
) -> list[PluginConformanceResult]:
    merged = []
    for result in results:
        issues = (*result.issues, *lifecycle_issues.get(result.name, ()))
        merged.append(
            PluginConformanceResult(
                name=result.name,
                valid=not any(issue.severity == "error" for issue in issues),
                issues=issues,
            )
        )
    return merged


def _settings_enabling(plugins: tuple[InfraPlugin, ...]) -> InfraSettings:
    return InfraSettings.model_validate(
        {
            "infra": {
                "plugins": {
                    _plugin_name(plugin): {"enabled": True}
                    for plugin in plugins
                    if _plugin_name(plugin) != "<unknown>"
                }
            }
        }
    )


def _duplicate_plugin_names(plugins: tuple[InfraPlugin, ...]) -> set[str]:
    names = [_plugin_name(plugin) for plugin in plugins]
    return {name for name in names if name != "<unknown>" and names.count(name) > 1}


def _plugin_name(plugin: object) -> str:
    metadata = getattr(plugin, "metadata", None)
    name = getattr(metadata, "name", None)
    return name if isinstance(name, str) and name else "<unknown>"


def _error(
    issues: list[PluginConformanceIssue],
    plugin: str,
    code: str,
    message: str,
) -> None:
    issues.append(PluginConformanceIssue(plugin, code, message))
