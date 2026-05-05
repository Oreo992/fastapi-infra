from pydantic import BaseModel, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata


class HTTPPluginConfig(BaseModel):
    base_url: str = ""
    timeout: float = 30.0
    headers: dict[str, str] = Field(default_factory=dict)


def _load_http_client():
    from infra.http.client import HttpClient

    return HttpClient


class HTTPPlugin:
    metadata = PluginMetadata(
        name="http",
        version="1.0.0",
        optional_dependencies=["aiohttp", "orjson"],
        default_enabled=False,
        provides=["http"],
    )
    config_model = HTTPPluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config if isinstance(ctx.config, HTTPPluginConfig) else HTTPPluginConfig()
        )
        client_type = _load_http_client()
        ctx.services["http"] = client_type(
            base_url=config.base_url,
            timeout=config.timeout,
            headers=config.headers,
        )

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        client = ctx.services.get("http")
        if client is not None:
            await client.close()

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("http", HealthState.HEALTHY)
