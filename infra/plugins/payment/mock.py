from uuid import uuid4

from pydantic import BaseModel

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata


class PaymentCheckout(BaseModel):
    id: str
    amount: int
    currency: str
    reference: str | None = None
    status: str
    url: str


class MockPaymentService:
    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
    ) -> PaymentCheckout:
        checkout_id = f"chk_{uuid4().hex}"
        return PaymentCheckout(
            id=checkout_id,
            amount=amount,
            currency=currency.upper(),
            reference=reference,
            status="pending",
            url=f"mock://checkout/{checkout_id}",
        )


class PaymentPlugin:
    metadata = PluginMetadata(
        name="payment",
        version="1.0.0",
        provides=["payment"],
    )
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["payment"] = MockPaymentService()

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("payment", HealthState.HEALTHY)
