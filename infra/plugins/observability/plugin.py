from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.observability.service import ObservabilityService


class ObservabilityPlugin:
    metadata = PluginMetadata(
        name="observability",
        version="1.0.0",
        provides=["observability"],
    )
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["observability"] = ObservabilityService(ctx.health)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("observability", HealthState.HEALTHY)
