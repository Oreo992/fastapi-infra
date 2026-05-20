from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.ai.adapters.anthropic import AnthropicAIProvider
from infra.plugins.ai.adapters.gemini import GeminiAIProvider
from infra.plugins.ai.adapters.openai import OpenAIProvider
from infra.plugins.ai.providers.base import AIProvider
from infra.plugins.ai.providers.mock import MockAIProvider
from infra.plugins.ai.registry import AIRegistry
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

AI_PROVIDER_ENTRY_POINT_GROUP = "fastapi_infra.ai_providers"
BUILTIN_REAL_AI_PROVIDERS = frozenset({"openai", "anthropic", "gemini"})


class AIProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = None
    timeout: float | None = Field(default=None, gt=0)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("base_url must be an absolute http(s) URL")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("api_key must be a non-empty string")
        return value

    @field_validator("timeout", mode="before")
    @classmethod
    def validate_timeout_type(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("timeout must be a positive number")
        return value

    def client_kwargs(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "api_key": self.api_key,
                "base_url": self.base_url,
                "timeout": self.timeout,
            }.items()
            if value is not None
        }


class AIPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: str = "mock"
    providers: dict[str, AIProviderConfig] = Field(default_factory=dict)
    health_probe: bool = False


class AIPlugin:
    metadata = PluginMetadata(
        name="ai",
        version="1.0.0",
        default_enabled=False,
        provides=["ai"],
    )
    config_model = AIPluginConfig
    manifest_hints = {
        "recommended_extras": ["ai"],
        "service_keys": {"ai": "infra.plugins.AI_SERVICE"},
        "env_vars": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"],
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
            "Configured real providers require health_probe=true and provider certification.",
        ],
    }

    def validate_config(self, config: AIPluginConfig | None) -> None:
        config = config if isinstance(config, AIPluginConfig) else AIPluginConfig()
        provider_names = set(config.providers) | {config.default_provider}
        external_provider_names_to_load(
            provider_kind="ai",
            requested_names=provider_names,
            registered_names={"mock", "openai", "anthropic", "gemini"},
            entry_point_group=AI_PROVIDER_ENTRY_POINT_GROUP,
        )

    def release_check(
        self,
        settings: InfraSettings,
        config: AIPluginConfig,
    ) -> list[PluginReleaseIssue]:
        issues: list[PluginReleaseIssue] = []
        provider_names = set(config.providers) | {config.default_provider}
        if config.default_provider == "mock":
            issues.append(
                release_error(
                    "mock_provider",
                    "production AI cannot use mock provider",
                )
            )
        for provider_name in sorted(provider_names & BUILTIN_REAL_AI_PROVIDERS):
            provider_config = config.providers.get(provider_name)
            if provider_config is None or not provider_config.api_key:
                issues.append(
                    release_error(
                        "api_key_required",
                        f"{provider_name} AI provider requires api_key in production config",
                    )
                )
        return issues

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: AIPluginConfig,
    ) -> list[PluginProviderCertification]:
        return [
            provider_certification("ai", provider_name)
            for provider_name in sorted({config.default_provider, *config.providers})
        ]

    def provider_release_policies(
        self,
        settings: InfraSettings,
        config: AIPluginConfig,
    ) -> list[PluginProviderPolicy]:
        return [
            provider_policy(
                "ai",
                {config.default_provider, *config.providers},
                local_providers={"mock"},
                health_probe=config.health_probe,
            )
        ]

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config if isinstance(ctx.config, AIPluginConfig) else AIPluginConfig()
        registry = AIRegistry(default_provider=config.default_provider)
        provider_names = set(config.providers) | {config.default_provider}
        for provider in _build_ai_providers(provider_names, config):
            registry.register(provider, default=provider.name == config.default_provider)
        registry.get()
        ctx.services["ai"] = registry

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        registry = ctx.services.get("ai")
        if isinstance(registry, AIRegistry):
            await registry.aclose()

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        config = ctx.config if isinstance(ctx.config, AIPluginConfig) else AIPluginConfig()
        registry = ctx.services.get("ai")
        if not isinstance(registry, AIRegistry):
            return ctx.health_status("ai", HealthState.UNHEALTHY, "ai registry missing")
        try:
            providers = [registry.get(name) for name in registry.names()]
        except LookupError as exc:
            return ctx.health_status("ai", HealthState.DEGRADED, str(exc))
        external_providers = [provider for provider in providers if provider.name != "mock"]
        if external_providers and config.health_probe:
            return await aggregate_provider_health_status(
                ctx,
                "ai",
                providers,
                local_provider_names={"mock"},
            )
        if external_providers:
            return ctx.health_status(
                "ai",
                HealthState.DEGRADED,
                "external provider configured; upstream is not checked by health",
                {"providers": [provider.name for provider in external_providers]},
            )
        return ctx.health_status("ai", HealthState.HEALTHY)


def _build_ai_providers(
    provider_names: set[str],
    config: AIPluginConfig,
) -> list[AIProvider]:
    providers: list[AIProvider] = []
    if "mock" in provider_names:
        providers.append(MockAIProvider())
    if "openai" in provider_names:
        providers.append(OpenAIProvider(config=_provider_config(config, "openai")))
    if "anthropic" in provider_names:
        providers.append(AnthropicAIProvider(config=_provider_config(config, "anthropic")))
    if "gemini" in provider_names:
        providers.append(GeminiAIProvider(config=_provider_config(config, "gemini")))

    for provider_name in external_provider_names_to_load(
        provider_kind="ai",
        requested_names=provider_names,
        registered_names={provider.name for provider in providers},
        entry_point_group=AI_PROVIDER_ENTRY_POINT_GROUP,
    ):
        providers.append(
            load_entry_point_provider(
                AI_PROVIDER_ENTRY_POINT_GROUP,
                provider_name,
                _provider_config(config, provider_name).model_dump(exclude_none=True),
                required_methods=("chat", "stream_chat", "embed"),
            )
        )
    return providers


def _provider_config(config: AIPluginConfig, provider_name: str) -> AIProviderConfig:
    return config.providers.get(provider_name, AIProviderConfig())
