from pydantic import BaseModel

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata


class CachePluginConfig(BaseModel):
    namespace: str = ""


def _load_cache_service():
    from infra.cache.service import CacheService

    return CacheService


class CachePlugin:
    metadata = PluginMetadata(
        name="cache",
        version="1.0.0",
        optional_dependencies=["orjson", "aiomysql", "redis"],
        default_enabled=False,
        provides=["cache"],
    )
    config_model = CachePluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config if isinstance(ctx.config, CachePluginConfig) else CachePluginConfig()
        )
        service_type = _load_cache_service()
        ctx.services["cache"] = service_type(namespace=config.namespace)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        cache = ctx.services.get("cache")
        if cache is not None:
            await cache._db_manager.close()

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("cache", HealthState.HEALTHY)
