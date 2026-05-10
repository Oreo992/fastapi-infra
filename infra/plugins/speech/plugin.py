from typing import Any

from pydantic import BaseModel, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.speech.providers.mock import MockSpeechProvider
from infra.plugins.speech.registry import SpeechProviderRegistry
from infra.plugins.speech.service import SpeechService


class SpeechPluginConfig(BaseModel):
    default_provider: str = "mock"
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SpeechPlugin:
    metadata = PluginMetadata(
        name="speech",
        version="1.0.0",
        provides=["speech"],
    )
    config_model = SpeechPluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config
            if isinstance(ctx.config, SpeechPluginConfig)
            else SpeechPluginConfig()
        )
        registry = SpeechProviderRegistry(default_provider=config.default_provider)
        provider_names = set(config.providers) | {config.default_provider}
        if "mock" in provider_names:
            registry.register(
                MockSpeechProvider(),
                default=config.default_provider == "mock",
            )
        unknown_providers = provider_names - {"mock"}
        if unknown_providers:
            raise ValueError(
                f"unknown speech provider: {', '.join(sorted(unknown_providers))}"
            )
        registry.get()
        ctx.services["speech"] = SpeechService(registry)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        service = ctx.services.get("speech")
        if not isinstance(service, SpeechService):
            return ctx.health_status("speech", HealthState.UNHEALTHY)
        try:
            service.registry.get()
        except LookupError as exc:
            return ctx.health_status("speech", HealthState.UNHEALTHY, str(exc))
        return ctx.health_status("speech", HealthState.HEALTHY)
