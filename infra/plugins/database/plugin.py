from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginReleaseIssue,
    provider_certification,
    release_error,
)


class DatabaseManagerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mysql_enabled: bool = True
    mysql_host: str = "localhost"
    mysql_port: int = Field(default=3306, gt=0)
    mysql_user: str = "root"
    mysql_password: str = Field(default="", repr=False)
    mysql_db: str = "test"
    mysql_pool_minsize: int = Field(default=10, ge=0)
    mysql_pool_maxsize: int = Field(default=100, gt=0)
    mysql_pool_recycle: int = Field(default=1800, gt=0)
    mysql_connect_timeout: float = Field(default=5.0, gt=0)
    redis_enabled: bool = True
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = Field(default=200, gt=0)
    redis_socket_connect_timeout: float = Field(default=3.0, gt=0)
    redis_socket_timeout: float = Field(default=10.0, gt=0)
    redis_health_check_interval: int = Field(default=30, ge=0)
    debug: bool = False


class DatabasePluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: Literal["memory", "connections"] = "memory"
    config: DatabaseManagerConfig = Field(default_factory=DatabaseManagerConfig)
    connect_on_startup: bool = False


def _load_database_manager():
    from infra.database.manager import DatabaseManager

    return DatabaseManager


def _load_memory_database_manager():
    from infra.database.memory import MemoryDatabaseManager

    return MemoryDatabaseManager


class DatabasePlugin:
    metadata = PluginMetadata(
        name="database",
        version="1.0.0",
        default_enabled=False,
        provides=["database"],
    )
    config_model = DatabasePluginConfig
    manifest_hints = {
        "recommended_extras": ["mysql", "redis"],
        "service_keys": {"database": "infra.plugins.DATABASE_SERVICE"},
        "env_vars": [
            "MYSQL_HOST",
            "MYSQL_PORT",
            "MYSQL_USER",
            "MYSQL_PASSWORD",
            "MYSQL_DATABASE",
            "REDIS_URL",
        ],
        "local_config_example": {
            "default_provider": "memory",
            "connect_on_startup": False,
            "config": {"mysql_enabled": False, "redis_enabled": False},
        },
        "production_config_example": {
            "default_provider": "connections",
            "connect_on_startup": True,
            "config": {
                "mysql_enabled": True,
                "mysql_host": "${MYSQL_HOST}",
                "mysql_port": "${MYSQL_PORT}",
                "mysql_user": "${MYSQL_USER}",
                "mysql_password": "${MYSQL_PASSWORD}",
                "mysql_db": "${MYSQL_DATABASE}",
                "redis_enabled": True,
                "redis_url": "${REDIS_URL}",
            },
        },
        "release_check_notes": [
            "Enabled MySQL and Redis connections require provider certification.",
        ],
    }

    def release_check(
        self,
        settings: InfraSettings,
        config: DatabasePluginConfig,
    ) -> list[PluginReleaseIssue]:
        if config.default_provider == "memory":
            return [
                release_error(
                    "connections_required",
                    "production database requires the connections provider",
                )
            ]
        if config.connect_on_startup:
            return []
        return [
            release_error(
                "connect_on_startup_required",
                "production database should connect on startup so broken credentials fail fast",
            )
        ]

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: DatabasePluginConfig,
    ) -> list[PluginProviderCertification]:
        if config.default_provider != "connections":
            return []
        providers: list[PluginProviderCertification] = []
        if config.config.mysql_enabled:
            providers.append(provider_certification("database", "mysql"))
        if config.config.redis_enabled:
            providers.append(provider_certification("database", "redis"))
        return providers

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config if isinstance(ctx.config, DatabasePluginConfig) else DatabasePluginConfig()
        )
        if config.default_provider == "memory":
            manager_type = _load_memory_database_manager()
            ctx.services["database"] = manager_type(config.config.model_dump(exclude_unset=True))
            return

        manager_type = _load_database_manager()
        ctx.services["database"] = manager_type(config.config.model_dump(exclude_unset=True))

    async def startup(self, ctx: PluginContext) -> None:
        config = (
            ctx.config if isinstance(ctx.config, DatabasePluginConfig) else DatabasePluginConfig()
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
        database = ctx.services.get("database")
        if database is None:
            return ctx.health_status("database", HealthState.UNHEALTHY, "database service missing")

        health_check = getattr(database, "health_check", None)
        if health_check is None:
            return ctx.health_status(
                "database",
                HealthState.DEGRADED,
                "database service does not expose health_check",
            )

        try:
            healthy = await health_check()
        except Exception as exc:
            return ctx.health_status("database", HealthState.UNHEALTHY, str(exc))

        if healthy:
            return ctx.health_status("database", HealthState.HEALTHY)
        return ctx.health_status("database", HealthState.UNHEALTHY, "database health check failed")
