from pydantic import BaseModel, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.auth.models import ApiKeyRecord
from infra.plugins.auth.service import AuthService
from infra.plugins.contract import PluginContext, PluginMetadata


class AuthPluginConfig(BaseModel):
    api_keys: dict[str, ApiKeyRecord] = Field(default_factory=dict)
    jwt_secret: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    access_token_ttl_seconds: int = 3600


class AuthPlugin:
    metadata = PluginMetadata(
        name="auth",
        version="1.0.0",
        provides=["auth"],
    )
    config_model = AuthPluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config if isinstance(ctx.config, AuthPluginConfig) else AuthPluginConfig()
        ctx.services["auth"] = AuthService(
            api_keys=config.api_keys,
            jwt_secret=config.jwt_secret,
            jwt_issuer=config.jwt_issuer,
            jwt_audience=config.jwt_audience,
            access_token_ttl_seconds=config.access_token_ttl_seconds,
        )

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("auth", HealthState.HEALTHY)
