from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.database.plugin import DatabaseManagerConfig, DatabasePluginConfig
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginReleaseIssue,
    enabled_plugin_config,
    provider_certification,
    release_error,
)


class CachePluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: Literal["memory", "redis"] = "memory"
    namespace: str = ""
    database_service: str = "database"
    database_config: DatabaseManagerConfig = Field(default_factory=DatabaseManagerConfig)


def _load_cache_service():
    from infra.cache.service import CacheService

    return CacheService


def _load_memory_cache_service():
    from infra.cache.service import MemoryCacheService

    return MemoryCacheService


def _load_database_manager():
    from infra.database.manager import DatabaseManager

    return DatabaseManager


class CachePlugin:
    metadata = PluginMetadata(
        name="cache",
        version="1.0.0",
        optional_dependencies=[],
        default_enabled=False,
        provides=["cache"],
    )
    config_model = CachePluginConfig
    manifest_hints = {
        "recommended_extras": ["redis"],
        "service_keys": {"cache": "infra.plugins.CACHE_SERVICE"},
        "service_references": {
            "database_service": {
                "default_service": "database",
                "optional": True,
                "description": (
                    "Uses an existing database service when available; otherwise "
                    "the cache plugin creates its own Redis-capable DatabaseManager."
                ),
            }
        },
        "env_vars": ["REDIS_URL"],
        "local_config_example": {
            "default_provider": "memory",
            "namespace": "",
        },
        "production_config_example": {
            "default_provider": "redis",
            "namespace": "app",
            "database_config": {
                "mysql_enabled": False,
                "redis_enabled": True,
                "redis_url": "${REDIS_URL}",
            },
        },
        "release_check_notes": [
            "Production cache requires Redis provider certification.",
        ],
    }

    def __init__(self) -> None:
        self._owned_database_managers: dict[int, Any] = {}

    def release_check(
        self,
        settings: InfraSettings,
        config: CachePluginConfig,
    ) -> list[PluginReleaseIssue]:
        redis_enabled = config.database_config.redis_enabled
        if config.default_provider == "memory":
            return [
                release_error(
                    "redis_required",
                    "production cache requires the redis provider",
                )
            ]
        if config.database_service == "database":
            database = enabled_plugin_config(settings, "database", DatabasePluginConfig)
            if database is not None:
                redis_enabled = database.config.redis_enabled
        if redis_enabled:
            return []
        return [
            release_error(
                "redis_required",
                "production cache requires Redis to be enabled",
            )
        ]

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: CachePluginConfig,
    ) -> list[PluginProviderCertification]:
        if config.default_provider != "redis":
            return []
        return [provider_certification("database", "redis")]

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config if isinstance(ctx.config, CachePluginConfig) else CachePluginConfig()
        if config.default_provider == "memory":
            service_type = _load_memory_cache_service()
            ctx.services["cache"] = service_type(namespace=config.namespace)
            return

        service_type = _load_cache_service()
        db_manager = ctx.services.get(config.database_service)
        if db_manager is None:
            manager_type = _load_database_manager()
            db_manager = manager_type(config.database_config.model_dump(exclude_unset=True))
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
        cache = ctx.services.get("cache")
        if cache is None:
            return ctx.health_status("cache", HealthState.UNHEALTHY, "cache service missing")

        checker = getattr(cache, "health_check", None)
        if checker is None:
            return ctx.health_status("cache", HealthState.HEALTHY)

        try:
            healthy = bool(await checker())
        except Exception as exc:
            return ctx.health_status("cache", HealthState.UNHEALTHY, str(exc))
        if not healthy:
            return ctx.health_status("cache", HealthState.UNHEALTHY, "cache health check failed")
        return ctx.health_status("cache", HealthState.HEALTHY)
