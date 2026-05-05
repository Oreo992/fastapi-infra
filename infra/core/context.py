from typing import Any

from fastapi import FastAPI

from infra.config.models import InfraSettings
from infra.core.health import HealthRegistry
from infra.plugins.contract import InfraPlugin
from infra.plugins.manager import PluginManager


class InfraContext:
    def __init__(
        self,
        app: FastAPI,
        settings: InfraSettings,
        plugins: list[InfraPlugin],
    ) -> None:
        self.app = app
        self.settings = settings
        self.plugin_manager = PluginManager(settings=settings, plugins=plugins)
        self.health: HealthRegistry = self.plugin_manager.health

    async def startup(self) -> None:
        await self.plugin_manager.startup()

    async def shutdown(self) -> None:
        await self.plugin_manager.shutdown()

    def get(self, name: str, default: Any = None) -> Any:
        return self.plugin_manager.get(name, default)
