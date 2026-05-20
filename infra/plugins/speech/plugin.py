from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.provider_extensions import (
    external_provider_names_to_load,
    load_entry_point_provider,
)
from infra.plugins.provider_health import aggregate_provider_health_status
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginProviderPolicy,
    PluginReleaseIssue,
    provider_certification,
    provider_policy,
    release_error,
)
from infra.plugins.speech.providers.mock import MockSpeechProvider
from infra.plugins.speech.providers.openai import (
    OpenAISpeechProvider,
    OpenAISpeechProviderConfig,
)
from infra.plugins.speech.registry import SpeechProviderRegistry
from infra.plugins.speech.service import SpeechService

SPEECH_PROVIDER_ENTRY_POINT_GROUP = "fastapi_infra.speech_providers"


class SpeechPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: str = "mock"
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    health_probe: bool = False


class SpeechPlugin:
    metadata = PluginMetadata(
        name="speech",
        version="1.0.0",
        default_enabled=False,
        provides=["speech"],
    )
    config_model = SpeechPluginConfig
    manifest_hints = {
        "service_keys": {"speech": "infra.plugins.SPEECH_SERVICE"},
        "env_vars": ["OPENAI_API_KEY"],
        "local_config_example": {
            "default_provider": "mock",
            "providers": {"mock": {}},
        },
        "production_config_example": {
            "default_provider": "openai",
            "health_probe": True,
            "providers": {"openai": {"api_key": "${OPENAI_API_KEY}"}},
        },
        "release_check_notes": [
            "Production cannot use the mock provider.",
            "OpenAI speech requires health_probe=true and provider certification.",
        ],
    }

    def validate_config(self, config: SpeechPluginConfig | None) -> None:
        config = config if isinstance(config, SpeechPluginConfig) else SpeechPluginConfig()
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "mock" in provider_names:
            registered_names.add("mock")
        if "openai" in provider_names:
            OpenAISpeechProviderConfig.model_validate(config.providers.get("openai", {}))
            registered_names.add("openai")
        external_provider_names_to_load(
            provider_kind="speech",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=SPEECH_PROVIDER_ENTRY_POINT_GROUP,
        )

    def release_check(
        self,
        settings: InfraSettings,
        config: SpeechPluginConfig,
    ) -> list[PluginReleaseIssue]:
        issues: list[PluginReleaseIssue] = []
        provider_names = set(config.providers) | {config.default_provider}
        if config.default_provider == "mock":
            issues.append(
                release_error(
                    "mock_provider",
                    "production speech cannot use mock provider",
                )
            )
        if "openai" in provider_names:
            try:
                OpenAISpeechProviderConfig.model_validate(config.providers.get("openai", {}))
            except (ValidationError, ValueError) as exc:
                issues.append(release_error("openai_config_invalid", str(exc)))
        return issues

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: SpeechPluginConfig,
    ) -> list[PluginProviderCertification]:
        return [
            provider_certification("speech", provider_name)
            for provider_name in sorted({config.default_provider, *config.providers})
        ]

    def provider_release_policies(
        self,
        settings: InfraSettings,
        config: SpeechPluginConfig,
    ) -> list[PluginProviderPolicy]:
        return [
            provider_policy(
                "speech",
                {config.default_provider, *config.providers},
                local_providers={"mock"},
                health_probe=config.health_probe,
            )
        ]

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config if isinstance(ctx.config, SpeechPluginConfig) else SpeechPluginConfig()
        registry = SpeechProviderRegistry(default_provider=config.default_provider)
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "mock" in provider_names:
            registry.register(
                MockSpeechProvider(),
                default=config.default_provider == "mock",
            )
            registered_names.add("mock")
        if "openai" in provider_names:
            registry.register(
                OpenAISpeechProvider(
                    OpenAISpeechProviderConfig.model_validate(config.providers.get("openai", {}))
                ),
                default=config.default_provider == "openai",
            )
            registered_names.add("openai")
        for provider_name in external_provider_names_to_load(
            provider_kind="speech",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=SPEECH_PROVIDER_ENTRY_POINT_GROUP,
        ):
            registry.register(
                load_entry_point_provider(
                    SPEECH_PROVIDER_ENTRY_POINT_GROUP,
                    provider_name,
                    config.providers.get(provider_name, {}),
                    required_methods=("transcribe", "synthesize"),
                ),
                default=config.default_provider == provider_name,
            )
        registry.get()
        ctx.services["speech"] = SpeechService(registry)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        config = ctx.config if isinstance(ctx.config, SpeechPluginConfig) else SpeechPluginConfig()
        service = ctx.services.get("speech")
        if not isinstance(service, SpeechService):
            return ctx.health_status("speech", HealthState.UNHEALTHY)
        try:
            providers = [service.registry.get(name) for name in service.registry.names()]
        except LookupError as exc:
            return ctx.health_status("speech", HealthState.UNHEALTHY, str(exc))
        external_providers = [provider for provider in providers if provider.name != "mock"]
        if external_providers and config.health_probe:
            return await aggregate_provider_health_status(
                ctx,
                "speech",
                providers,
                local_provider_names={"mock"},
            )
        if external_providers:
            return ctx.health_status(
                "speech",
                HealthState.DEGRADED,
                "external provider configured; upstream is not checked by health",
                {"providers": [provider.name for provider in external_providers]},
            )
        return ctx.health_status("speech", HealthState.HEALTHY)
