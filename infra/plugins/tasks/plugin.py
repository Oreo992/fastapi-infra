from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.tasks.adapters.memory import MemoryTaskQueue


class TasksPlugin:
    metadata = PluginMetadata(
        name="tasks",
        version="1.0.0",
        provides=["tasks"],
    )
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["tasks"] = MemoryTaskQueue()

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("tasks", HealthState.HEALTHY)
