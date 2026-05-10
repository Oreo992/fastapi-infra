from typing import Any

from pydantic import BaseModel, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.payment.mock import MockPaymentProvider
from infra.plugins.payment.registry import PaymentProviderRegistry
from infra.plugins.payment.service import PaymentService


class PaymentPluginConfig(BaseModel):
    default_provider: str = "mock"
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PaymentPlugin:
    metadata = PluginMetadata(
        name="payment",
        version="1.0.0",
        provides=["payment"],
    )
    config_model = PaymentPluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config
            if isinstance(ctx.config, PaymentPluginConfig)
            else PaymentPluginConfig()
        )
        registry = PaymentProviderRegistry(default_provider=config.default_provider)
        provider_names = set(config.providers) or {"mock"}
        if "mock" in provider_names:
            registry.register(
                MockPaymentProvider(),
                default=config.default_provider == "mock",
            )
        unknown_providers = provider_names - {"mock"}
        if unknown_providers:
            raise ValueError(
                f"unknown payment provider: {', '.join(sorted(unknown_providers))}"
            )
        registry.get()
        ctx.services["payment"] = PaymentService(registry)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        service = ctx.services.get("payment")
        if not isinstance(service, PaymentService):
            return ctx.health_status("payment", HealthState.UNHEALTHY)
        try:
            service.registry.get()
        except LookupError as exc:
            return ctx.health_status("payment", HealthState.UNHEALTHY, str(exc))
        return ctx.health_status("payment", HealthState.HEALTHY)
