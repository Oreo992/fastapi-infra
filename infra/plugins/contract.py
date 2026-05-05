from typing import Any, Protocol

from pydantic import BaseModel, Field

from infra.config.models import InfraSettings, PluginSettings
from infra.core.health import HealthRegistry, HealthState, HealthStatus


class PluginMetadata(BaseModel):
    name: str
    version: str
    dependencies: list[str] = Field(default_factory=list)
    optional_dependencies: list[str] = Field(default_factory=list)
    default_enabled: bool | None = None
    provides: list[str] = Field(default_factory=list)


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
    metadata: PluginMetadata
    config_model: type[BaseModel] | None

    def register(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def startup(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def shutdown(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        raise NotImplementedError
