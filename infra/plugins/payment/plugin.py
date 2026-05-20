from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.database.plugin import DatabasePluginConfig
from infra.plugins.payment.mock import MockPaymentProvider
from infra.plugins.payment.registry import PaymentProviderRegistry
from infra.plugins.payment.service import PaymentService
from infra.plugins.payment.store import PAYMENT_STORE_SCHEMA_SQL, SqlPaymentStore
from infra.plugins.payment.stripe import StripePaymentProvider, StripeProviderConfig
from infra.plugins.provider_extensions import (
    external_provider_names_to_load,
    load_entry_point_provider,
)
from infra.plugins.provider_health import aggregate_provider_health_status
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginProviderPolicy,
    PluginReleaseDependency,
    PluginReleaseIssue,
    enabled_plugin_config,
    provider_certification,
    provider_policy,
    release_dependency,
    release_error,
)

PAYMENT_PROVIDER_ENTRY_POINT_GROUP = "fastapi_infra.payment_providers"


class PaymentPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: str = "mock"
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    store_service: Literal["database"] | None = None
    health_probe: bool = False


class PaymentPlugin:
    metadata = PluginMetadata(
        name="payment",
        version="1.0.0",
        default_enabled=False,
        provides=["payment"],
    )
    config_model = PaymentPluginConfig
    manifest_hints = {
        "service_keys": {"payment": "infra.plugins.PAYMENT_SERVICE"},
        "service_references": {
            "store_service": {
                "default_service": "database",
                "required_when": "default_provider != 'mock' in production",
                "required_unless_config": {"default_provider": "mock"},
                "description": "Database service used for durable checkout/refund storage.",
            }
        },
        "env_vars": ["STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET"],
        "local_config_example": {
            "default_provider": "mock",
        },
        "production_config_example": {
            "default_provider": "stripe",
            "health_probe": True,
            "store_service": "database",
            "providers": {
                "stripe": {
                    "api_key": "${STRIPE_API_KEY}",
                    "webhook_secret": "${STRIPE_WEBHOOK_SECRET}",
                }
            },
        },
        "production_dependencies": ["database", "webhooks"],
        "migrations": [
            {
                "version": "00000000001000",
                "name": "infra_payment_store",
                "sql": PAYMENT_STORE_SCHEMA_SQL,
            }
        ],
        "release_check_notes": [
            "Production cannot use the mock provider.",
            "Stripe requires webhook_secret, health_probe=true, provider certification, and a MySQL-backed database store.",
        ],
    }

    def validate_config(self, config: PaymentPluginConfig | None) -> None:
        config = config if isinstance(config, PaymentPluginConfig) else PaymentPluginConfig()
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "mock" in provider_names:
            registered_names.add("mock")
        if "stripe" in provider_names:
            StripeProviderConfig.model_validate(config.providers.get("stripe", {}))
            registered_names.add("stripe")
        external_provider_names_to_load(
            provider_kind="payment",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=PAYMENT_PROVIDER_ENTRY_POINT_GROUP,
        )

    def release_check(
        self,
        settings: InfraSettings,
        config: PaymentPluginConfig,
    ) -> list[PluginReleaseIssue]:
        issues: list[PluginReleaseIssue] = []
        provider_names = set(config.providers) | {config.default_provider}
        if config.default_provider == "mock":
            issues.append(
                release_error(
                    "mock_provider",
                    "production payment cannot use mock provider",
                )
            )
        else:
            issues.extend(_durable_store_issues(settings, config))
        if "stripe" in provider_names:
            try:
                stripe_config = StripeProviderConfig.model_validate(
                    config.providers.get("stripe", {})
                )
            except (ValidationError, ValueError) as exc:
                issues.append(release_error("stripe_config_invalid", str(exc)))
            else:
                if not stripe_config.webhook_secret:
                    issues.append(
                        release_error(
                            "stripe_webhook_secret_required",
                            "Stripe production config should include webhook_secret",
                        )
                    )
        return issues

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: PaymentPluginConfig,
    ) -> list[PluginProviderCertification]:
        return [
            provider_certification("payment", provider_name)
            for provider_name in sorted({config.default_provider, *config.providers})
        ]

    def provider_release_policies(
        self,
        settings: InfraSettings,
        config: PaymentPluginConfig,
    ) -> list[PluginProviderPolicy]:
        return [
            provider_policy(
                "payment",
                {config.default_provider, *config.providers},
                local_providers={"mock"},
                health_probe=config.health_probe,
            )
        ]

    def release_dependencies(
        self,
        settings: InfraSettings,
        config: PaymentPluginConfig,
    ) -> list[PluginReleaseDependency]:
        provider_names = set(config.providers) | {config.default_provider}
        if "stripe" not in provider_names:
            return []
        return [
            release_dependency(
                "webhooks",
                "stripe_webhook_provider_required",
                "Stripe payment requires webhooks.providers.stripe",
                config_path="providers.stripe",
            ),
            release_dependency(
                "webhooks",
                "stripe_webhook_required_provider_required",
                "Stripe payment requires webhooks.required_providers to include stripe",
                config_path="required_providers",
                contains="stripe",
            ),
        ]

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config if isinstance(ctx.config, PaymentPluginConfig) else PaymentPluginConfig()
        )
        registry = PaymentProviderRegistry(default_provider=config.default_provider)
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "mock" in provider_names:
            registry.register(
                MockPaymentProvider(),
                default=config.default_provider == "mock",
            )
            registered_names.add("mock")
        if "stripe" in provider_names:
            stripe_config = StripeProviderConfig.model_validate(config.providers.get("stripe", {}))
            registry.register(
                StripePaymentProvider(stripe_config),
                default=config.default_provider == "stripe",
            )
            registered_names.add("stripe")
        for provider_name in external_provider_names_to_load(
            provider_kind="payment",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=PAYMENT_PROVIDER_ENTRY_POINT_GROUP,
        ):
            registry.register(
                load_entry_point_provider(
                    PAYMENT_PROVIDER_ENTRY_POINT_GROUP,
                    provider_name,
                    config.providers.get(provider_name, {}),
                    required_methods=(
                        "create_checkout",
                        "get_checkout",
                        "get_payment_status",
                        "create_refund",
                    ),
                ),
                default=config.default_provider == provider_name,
            )
        registry.get()
        store = None
        if config.store_service is not None:
            database = ctx.services.get(config.store_service)
            if database is None:
                raise RuntimeError(
                    f"payment store service is not available: {config.store_service}"
                )
            if not callable(getattr(database, "execute_sql", None)):
                raise RuntimeError(
                    f"payment store service must expose execute_sql: {config.store_service}"
                )
            store = SqlPaymentStore(database)
        ctx.services["payment"] = PaymentService(registry, store=store)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        config = (
            ctx.config if isinstance(ctx.config, PaymentPluginConfig) else PaymentPluginConfig()
        )
        service = ctx.services.get("payment")
        if not isinstance(service, PaymentService):
            return ctx.health_status("payment", HealthState.UNHEALTHY)
        try:
            providers = [service.registry.get(name) for name in service.registry.names()]
        except LookupError as exc:
            return ctx.health_status("payment", HealthState.UNHEALTHY, str(exc))
        external_providers = [provider for provider in providers if provider.name != "mock"]
        if external_providers and config.health_probe:
            return await aggregate_provider_health_status(
                ctx,
                "payment",
                providers,
                local_provider_names={"mock"},
            )
        if external_providers:
            return ctx.health_status(
                "payment",
                HealthState.DEGRADED,
                "external provider configured; upstream is not checked by health",
                {"providers": [provider.name for provider in external_providers]},
            )
        return ctx.health_status("payment", HealthState.HEALTHY)


def _durable_store_issues(
    settings: InfraSettings,
    config: PaymentPluginConfig,
) -> list[PluginReleaseIssue]:
    if config.store_service is None:
        return [
            release_error(
                "durable_store_required",
                "production payment should configure store_service for durable provider results",
            )
        ]
    if config.store_service != "database":
        return []

    database = enabled_plugin_config(settings, "database", DatabasePluginConfig)
    if database is None:
        return [
            release_error(
                "durable_database_store_required",
                "payment store_service='database' requires the database plugin to be enabled",
            )
        ]
    if database.config.mysql_enabled:
        return []
    return [
        release_error(
            "durable_database_store_required",
            "payment store_service='database' requires MySQL to be enabled",
        )
    ]
