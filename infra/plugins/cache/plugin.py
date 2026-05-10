from typing import Any

from pydantic import BaseModel, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata


class CachePluginConfig(BaseModel):
    namespace: str = ""
    database_service: str = "database"
    database_config: dict[str, Any] = Field(default_factory=dict)


def _load_cache_service():
    from infra.cache.service import CacheService

    return CacheService


def _load_database_manager():
    from infra.database.manager import DatabaseManager

    return DatabaseManager


class CachePlugin:
    metadata = PluginMetadata(
        name="cache",
        version="1.0.0",
        optional_dependencies=["orjson", "aiomysql", "redis"],
        default_enabled=False,
        provides=["cache"],
    )
    config_model = CachePluginConfig

    def __init__(self) -> None:
        self._owned_database_managers: dict[int, Any] = {}

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config if isinstance(ctx.config, CachePluginConfig) else CachePluginConfig()
        )
        service_type = _load_cache_service()
        db_manager = ctx.services.get(config.database_service)
        if db_manager is None:
            manager_type = _load_database_manager()
            db_manager = manager_type(config.database_config)
            self._owned_database_managers[id(ctx)] = db_manager
        ctx.services["cache"] = service_type(
            namespace=config.namespace,
            db_manager=db_manager,
        )

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        owned_manager = self._owned_database_managers.pop(id(ctx), None)
        if owned_manager is not None:
            await owned_manager.close()

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("cache", HealthState.HEALTHY)
