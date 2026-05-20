from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.provider_extensions import (
    external_provider_names_to_load,
    load_entry_point_provider,
)
from infra.plugins.release_checks import (
    PluginReleaseIssue,
    release_error,
)
from infra.plugins.webhooks.providers import (
    StripeWebhookProvider,
    StripeWebhookProviderConfig,
    WebhookProviderRegistry,
)
from infra.plugins.webhooks.store import WEBHOOK_STORE_SCHEMA_SQL

WebhookHandler = Callable[[str, Any], Awaitable[Any]]
WEBHOOK_PROVIDER_ENTRY_POINT_GROUP = "fastapi_infra.webhook_providers"


class WebhooksPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    durable_store: bool = False
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    required_providers: list[str] = Field(default_factory=list)

    @field_validator("required_providers")
    @classmethod
    def normalize_required_providers(cls, value: list[str]) -> list[str]:
        providers: list[str] = []
        seen: set[str] = set()
        for provider in value:
            normalized = provider.strip().lower()
            if not normalized:
                raise ValueError("required_providers must not contain empty provider names")
            if normalized not in seen:
                providers.append(normalized)
                seen.add(normalized)
        return providers


class WebhookDispatcher:
    def __init__(
        self,
        *,
        durable_store_required: bool = False,
        provider_registry: WebhookProviderRegistry | None = None,
        required_providers: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._handlers: list[WebhookHandler] = []
        self.durable_store_required = durable_store_required
        self.provider_registry = provider_registry or WebhookProviderRegistry()
        self.required_providers = frozenset(
            provider.strip().lower()
            for provider in (required_providers or set())
            if provider.strip()
        )

    def register(self, handler: WebhookHandler) -> None:
        self._handlers.append(handler)

    async def dispatch(self, event: str, payload: Any) -> list[Any]:
        return [await handler(event, payload) for handler in self._handlers]


class WebhooksPlugin:
    metadata = PluginMetadata(
        name="webhooks",
        version="1.0.0",
        default_enabled=False,
        provides=["webhooks"],
    )
    config_model = WebhooksPluginConfig
    manifest_hints = {
        "service_keys": {"webhooks": "infra.plugins.WEBHOOKS_SERVICE"},
        "env_vars": ["STRIPE_WEBHOOK_SECRET"],
        "local_config_example": {
            "durable_store": False,
            "providers": {},
            "required_providers": [],
        },
        "production_config_example": {
            "durable_store": True,
            "providers": {
                "stripe": {
                    "webhook_secret": "${STRIPE_WEBHOOK_SECRET}",
                }
            },
            "required_providers": ["stripe"],
        },
        "migrations": [
            {
                "version": "00000000001100",
                "name": "infra_webhook_store",
                "sql": WEBHOOK_STORE_SCHEMA_SQL,
            }
        ],
        "release_check_notes": [
            "Production webhooks should enable durable_store and explicit signed providers.",
        ],
    }

    def validate_config(self, config: WebhooksPluginConfig | None) -> None:
        config = config if isinstance(config, WebhooksPluginConfig) else WebhooksPluginConfig()
        provider_names = set(config.providers) | set(config.required_providers)
        registered_names: set[str] = set()
        if "stripe" in provider_names:
            StripeWebhookProviderConfig.model_validate(config.providers.get("stripe", {}))
            registered_names.add("stripe")
        external_provider_names_to_load(
            provider_kind="webhook",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=WEBHOOK_PROVIDER_ENTRY_POINT_GROUP,
        )

    def release_check(
        self,
        settings: InfraSettings,
        config: WebhooksPluginConfig,
    ) -> list[PluginReleaseIssue]:
        issues: list[PluginReleaseIssue] = []
        if not config.durable_store:
            issues.append(
                release_error(
                    "durable_store_required",
                    "production webhook routes should be installed with a durable WebhookStore",
                )
            )
        if not config.providers:
            issues.append(
                release_error(
                    "providers_required",
                    "production webhook routes should declare signed providers",
                )
            )
        if not config.required_providers:
            issues.append(
                release_error(
                    "required_providers_required",
                    "production webhook routes should declare required_providers",
                )
            )
        missing_required = set(config.required_providers) - set(config.providers)
        if missing_required:
            issues.append(
                release_error(
                    "required_provider_missing",
                    "required webhook provider is not configured: "
                    + ", ".join(sorted(missing_required)),
                )
            )
        if "stripe" in config.providers:
            try:
                StripeWebhookProviderConfig.model_validate(config.providers.get("stripe", {}))
            except (ValidationError, ValueError) as exc:
                issues.append(release_error("stripe_provider_invalid", str(exc)))
        return issues

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config if isinstance(ctx.config, WebhooksPluginConfig) else WebhooksPluginConfig()
        )
        registry = WebhookProviderRegistry()
        provider_names = set(config.providers) | set(config.required_providers)
        registered_names: set[str] = set()
        if "stripe" in provider_names:
            stripe_config = StripeWebhookProviderConfig.model_validate(
                config.providers.get("stripe", {})
            )
            registry.register(StripeWebhookProvider(stripe_config))
            registered_names.add("stripe")
        for provider_name in external_provider_names_to_load(
            provider_kind="webhook",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=WEBHOOK_PROVIDER_ENTRY_POINT_GROUP,
        ):
            registry.register(
                load_entry_point_provider(
                    WEBHOOK_PROVIDER_ENTRY_POINT_GROUP,
                    provider_name,
                    config.providers.get(provider_name, {}),
                    required_methods=("verify", "build_event"),
                )
            )
        ctx.services["webhooks"] = WebhookDispatcher(
            durable_store_required=config.durable_store,
            provider_registry=registry,
            required_providers=set(config.required_providers),
        )

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("webhooks", HealthState.HEALTHY)
