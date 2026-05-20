from typing import Any, TypeVar, overload

from fastapi import FastAPI

from infra.config.models import InfraSettings
from infra.core.health import HealthRegistry, HealthStatus
from infra.core.services import ServiceKey
from infra.plugins.contract import InfraPlugin
from infra.plugins.manager import PluginManager

T = TypeVar("T")
_MISSING = object()


class InfraContext:
    def __init__(
        self,
        app: FastAPI,
        settings: InfraSettings,
        plugins: list[InfraPlugin],
        *,
        health_check_timeout_seconds: float | None = 5.0,
    ) -> None:
        self.app = app
        self.settings = settings
        self.plugin_manager = PluginManager(
            settings=settings,
            plugins=plugins,
            health_check_timeout_seconds=health_check_timeout_seconds,
        )
        self.health: HealthRegistry = self.plugin_manager.health

    async def startup(self) -> None:
        await self.plugin_manager.startup()

    async def shutdown(self) -> None:
        await self.plugin_manager.shutdown()

    async def refresh_health(
        self,
        *,
        timeout_seconds: float | None = 5.0,
    ) -> dict[str, HealthStatus]:
        return await self.plugin_manager.refresh_health(timeout_seconds=timeout_seconds)

    @overload
    def get(self, name: ServiceKey[T]) -> T | None: ...

    @overload
    def get(self, name: ServiceKey[T], default: T) -> T: ...

    @overload
    def get(self, name: str, default: Any = None) -> Any: ...

    def get(self, name: str | ServiceKey[T], default: Any = None) -> Any:
        if isinstance(name, ServiceKey):
            service = self.plugin_manager.get(name.name, default)
            if service is default:
                return service
            return name.validate(service)
        return self.plugin_manager.get(name, default)

    @overload
    def require(self, name: ServiceKey[T]) -> T: ...

    @overload
    def require(self, name: str) -> Any: ...

    def require(self, name: str | ServiceKey[T]) -> Any:
        service_name = name.name if isinstance(name, ServiceKey) else name.strip()
        if not service_name:
            raise ValueError("service name must not be empty")
        service = self.plugin_manager.get(service_name, _MISSING)
        if service is _MISSING:
            raise RuntimeError(f"infra service is not available: {service_name}")
        if isinstance(name, ServiceKey):
            return name.validate(service)
        return service
