from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.tasks.adapters.memory import MemoryTaskQueue
from infra.plugins.tasks.adapters.redis_stream import RedisStreamTaskQueue


class TasksPluginConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    adapter: Literal["memory", "redis"] = "memory"
    service: str = "tasks"
    redis: Any | None = None
    database_service: str = "database"
    stream_name: str = "infra:tasks"
    consumer_group: str = "infra"
    consumer_name: str = "tasks"
    pending_min_idle_ms: int = 60_000


class TasksPlugin:
    metadata = PluginMetadata(
        name="tasks",
        version="1.0.0",
        provides=["tasks"],
    )
    config_model = TasksPluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = self._config(ctx)
        if config.adapter == "memory":
            ctx.services[config.service] = MemoryTaskQueue()
            return

        if config.redis is not None:
            ctx.services[config.service] = RedisStreamTaskQueue(
                config.redis,
                stream_name=config.stream_name,
                consumer_group=config.consumer_group,
                consumer_name=config.consumer_name,
                pending_min_idle_ms=config.pending_min_idle_ms,
            )

    async def startup(self, ctx: PluginContext) -> None:
        config = self._config(ctx)
        if config.adapter != "redis" or config.service in ctx.services:
            return None

        database = ctx.services.get(config.database_service)
        redis_factory = getattr(database, "_get_or_create_redis_client", None)
        if database is None or redis_factory is None:
            raise RuntimeError(
                "Redis task adapter requires a redis client or a database service "
                "with _get_or_create_redis_client()."
            )

        redis = await redis_factory()
        ctx.services[config.service] = RedisStreamTaskQueue(
            redis,
            stream_name=config.stream_name,
            consumer_group=config.consumer_group,
            consumer_name=config.consumer_name,
            pending_min_idle_ms=config.pending_min_idle_ms,
        )
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("tasks", HealthState.HEALTHY)

    def _config(self, ctx: PluginContext) -> TasksPluginConfig:
        if isinstance(ctx.config, TasksPluginConfig):
            return ctx.config
        return TasksPluginConfig()
