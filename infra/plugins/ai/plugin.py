from typing import Literal

from pydantic import BaseModel

from infra.core.health import HealthState
from infra.plugins.ai.adapters.anthropic import AnthropicAIProvider
from infra.plugins.ai.adapters.gemini import GeminiAIProvider
from infra.plugins.ai.adapters.openai import OpenAIProvider
from infra.plugins.ai.providers.mock import MockAIProvider
from infra.plugins.ai.registry import AIRegistry
from infra.plugins.contract import PluginContext, PluginMetadata


class AIPluginConfig(BaseModel):
    default_provider: Literal["mock", "openai", "anthropic", "gemini"] = "mock"


class AIPlugin:
    metadata = PluginMetadata(
        name="ai",
        version="1.0.0",
        default_enabled=True,
        provides=["ai"],
    )
    config_model = AIPluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config if isinstance(ctx.config, AIPluginConfig) else AIPluginConfig()
        registry = AIRegistry(default_provider=config.default_provider)
        providers = [
            MockAIProvider(),
            OpenAIProvider(),
            AnthropicAIProvider(),
            GeminiAIProvider(),
        ]
        for provider in providers:
            registry.register(provider, default=provider.name == config.default_provider)
        ctx.services["ai"] = registry

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        registry = ctx.services.get("ai")
        if not isinstance(registry, AIRegistry):
            return ctx.health_status("ai", HealthState.UNHEALTHY, "ai registry missing")
        try:
            registry.get()
        except LookupError as exc:
            return ctx.health_status("ai", HealthState.DEGRADED, str(exc))
        return ctx.health_status("ai", HealthState.HEALTHY)
