from collections.abc import Iterable, Mapping
from typing import Any, Protocol, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, field_validator

from infra.config.models import InfraSettings, PluginSettings
from infra.core.health import HealthRegistry, HealthState, HealthStatus
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginProviderPolicy,
    PluginReleaseDependency,
)


class PluginMetadata(BaseModel):
    name: str
    version: str
    dependencies: list[str] = Field(default_factory=list)
    optional_dependencies: list[str] = Field(default_factory=list)
    default_enabled: bool = False
    provides: list[str] = Field(default_factory=list)
    service_name_config: str | None = None


class PluginMigrationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^[0-9]{14}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    sql: str = Field(min_length=1)


class PluginScaffoldFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    content: str
    executable: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("scaffold file path must not be empty")
        if path.startswith("/") or "\\" in path:
            raise ValueError("scaffold file path must be a relative POSIX path")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("scaffold file path must not contain empty, '.', or '..' parts")
        return path


class PluginServiceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_service: str | None = None
    required_when: str | None = None
    required_when_config: dict[str, Any] = Field(default_factory=dict)
    required_unless_config: dict[str, Any] = Field(default_factory=dict)
    optional: bool = False
    description: str = ""


class PluginManifestHints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_extras: list[str] = Field(default_factory=list)
    env_vars: list[str] = Field(default_factory=list)
    local_config_example: dict[str, Any] = Field(default_factory=dict)
    production_config_example: dict[str, Any] = Field(default_factory=dict)
    production_dependencies: list[str] = Field(default_factory=list)
    service_keys: dict[str, str] = Field(default_factory=dict)
    service_references: dict[str, PluginServiceReference] = Field(default_factory=dict)
    migrations: list[PluginMigrationSpec] = Field(default_factory=list)
    scaffold_files: list[PluginScaffoldFile] = Field(default_factory=list)
    scaffold_readme_sections: list[str] = Field(default_factory=list)
    release_check_notes: list[str] = Field(default_factory=list)


class PluginContext(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    settings: InfraSettings
    plugin_settings: PluginSettings
    services: dict[str, Any]
    health: HealthRegistry
    config: BaseModel | None = None

    def health_status(
        self,
        name: str,
        status: HealthState,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> HealthStatus:
        return HealthStatus(
            name=name,
            status=status,
            message=message,
            details=details or {},
        )


class InfraPlugin(Protocol):
    @property
    def metadata(self) -> PluginMetadata:
        raise NotImplementedError

    @property
    def config_model(self) -> type[BaseModel] | None:
        raise NotImplementedError

    def register(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def startup(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def shutdown(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        raise NotImplementedError


class PluginReleaseCheckHook(Protocol):
    def release_check(
        self,
        settings: InfraSettings,
        config: Any,
    ) -> Iterable[object] | None:
        raise NotImplementedError


class PluginReleaseDependencyHook(Protocol):
    def release_dependencies(
        self,
        settings: InfraSettings,
        config: Any,
    ) -> Iterable[PluginReleaseDependency] | None:
        raise NotImplementedError


class PluginProviderCertificationHook(Protocol):
    def provider_certifications(
        self,
        settings: InfraSettings,
        config: Any,
    ) -> Iterable[PluginProviderCertification] | None:
        raise NotImplementedError


class PluginProviderPolicyHook(Protocol):
    def provider_release_policies(
        self,
        settings: InfraSettings,
        config: Any,
    ) -> Iterable[PluginProviderPolicy] | None:
        raise NotImplementedError


class PluginManifestHintsHook(Protocol):
    manifest_hints: Mapping[str, Any] | PluginManifestHints


class PluginConfigValidatorHook(Protocol):
    def validate_config(self, config: Any) -> None:
        raise NotImplementedError


def resolve_plugin_manifest_hints(plugin: object) -> PluginManifestHints:
    if _has_manifest_hints(plugin):
        return PluginManifestHints.model_validate(plugin.manifest_hints)
    return PluginManifestHints()


def _has_manifest_hints(plugin: object) -> TypeGuard[PluginManifestHintsHook]:
    return hasattr(plugin, "manifest_hints")
