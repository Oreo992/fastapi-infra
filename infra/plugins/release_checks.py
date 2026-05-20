import contextlib
from collections.abc import Iterable
from typing import Any, Literal, NotRequired, TypedDict, TypeVar

from pydantic import BaseModel, ValidationError

from infra.config.models import InfraSettings

PluginReleaseSeverity = Literal["error", "warning"]


class PluginReleaseIssue(TypedDict):
    code: str
    message: str
    plugin: NotRequired[str]
    severity: NotRequired[PluginReleaseSeverity]


class PluginProviderCertification(TypedDict):
    provider_kind: str
    provider_name: str


class PluginProviderPolicy(TypedDict):
    provider_kind: str
    declared_providers: list[str]
    local_providers: list[str]
    health_probe: bool


class PluginReleaseDependency(TypedDict):
    plugin: str
    code: str
    message: str
    config_path: NotRequired[str]
    contains: NotRequired[Any]
    equals: NotRequired[Any]
    severity: NotRequired[PluginReleaseSeverity]


ConfigT = TypeVar("ConfigT", bound=BaseModel)
_UNSET = object()


def release_error(code: str, message: str) -> PluginReleaseIssue:
    return {"code": code, "message": message, "severity": "error"}


def release_warning(code: str, message: str) -> PluginReleaseIssue:
    return {"code": code, "message": message, "severity": "warning"}


def provider_certification(
    provider_kind: str,
    provider_name: str,
) -> PluginProviderCertification:
    return {"provider_kind": provider_kind, "provider_name": provider_name}


def provider_policy(
    provider_kind: str,
    declared_providers: Iterable[str],
    *,
    local_providers: Iterable[str],
    health_probe: bool,
) -> PluginProviderPolicy:
    return {
        "provider_kind": provider_kind,
        "declared_providers": sorted(declared_providers),
        "local_providers": sorted(local_providers),
        "health_probe": health_probe,
    }


def release_dependency(
    plugin: str,
    code: str,
    message: str,
    *,
    config_path: str | None = None,
    contains: Any = _UNSET,
    equals: Any = _UNSET,
    severity: PluginReleaseSeverity = "error",
) -> PluginReleaseDependency:
    dependency: PluginReleaseDependency = {
        "plugin": plugin,
        "code": code,
        "message": message,
        "severity": severity,
    }
    if config_path is not None:
        dependency["config_path"] = config_path
    if contains is not _UNSET:
        dependency["contains"] = contains
    if equals is not _UNSET:
        dependency["equals"] = equals
    return dependency


def enabled_plugin_config(
    settings: InfraSettings,
    plugin_name: str,
    config_model: type[ConfigT],
) -> ConfigT | None:
    plugin_settings = settings.infra.plugins.get(plugin_name)
    if plugin_settings is None or plugin_settings.enabled is not True:
        return None
    with contextlib.suppress(ValidationError, ValueError):
        return config_model.model_validate(plugin_settings.config)
    return None
