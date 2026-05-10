from infra.plugins.payment.models import PaymentCheckout
from infra.plugins.payment.registry import PaymentProviderRegistry


class PaymentService:
    def __init__(self, registry: PaymentProviderRegistry) -> None:
        self.registry = registry

    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
        provider: str | None = None,
    ) -> PaymentCheckout:
        payment_provider = self.registry.get(provider)
        return await payment_provider.create_checkout(
            amount=amount,
            currency=currency,
            reference=reference,
        )

    async def get_payment_status(
        self,
        checkout_id: str,
        provider: str | None = None,
    ) -> str:
        payment_provider = self.registry.get(provider)
        return await payment_provider.get_payment_status(checkout_id)
