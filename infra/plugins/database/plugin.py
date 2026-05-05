from typing import Any

from pydantic import BaseModel, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata


class DatabasePluginConfig(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    connect_on_startup: bool = False


def _load_database_manager():
    from infra.database.manager import DatabaseManager

    return DatabaseManager


class DatabasePlugin:
    metadata = PluginMetadata(
        name="database",
        version="1.0.0",
        optional_dependencies=["aiomysql", "redis"],
        default_enabled=False,
        provides=["database"],
    )
    config_model = DatabasePluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config
            if isinstance(ctx.config, DatabasePluginConfig)
            else DatabasePluginConfig()
        )
        manager_type = _load_database_manager()
        ctx.services["database"] = manager_type(config.config)

    async def startup(self, ctx: PluginContext) -> None:
        config = (
            ctx.config
            if isinstance(ctx.config, DatabasePluginConfig)
            else DatabasePluginConfig()
        )
        if config.connect_on_startup:
            database = ctx.services.get("database")
            if database is not None:
                await database.initialize()

    async def shutdown(self, ctx: PluginContext) -> None:
        database = ctx.services.get("database")
        if database is not None:
            await database.close()

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("database", HealthState.HEALTHY)
