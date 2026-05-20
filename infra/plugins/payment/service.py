from infra.plugins.payment.models import PaymentCheckout, PaymentRefund
from infra.plugins.payment.registry import PaymentProviderRegistry
from infra.plugins.payment.store import PaymentStore


class PaymentService:
    def __init__(
        self,
        registry: PaymentProviderRegistry,
        store: PaymentStore | None = None,
    ) -> None:
        self.registry = registry
        self.store = store

    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
        provider: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentCheckout:
        payment_provider = self.registry.get(provider)
        checkout = await payment_provider.create_checkout(
            amount=amount,
            currency=currency,
            reference=reference,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            provider_options=provider_options,
        )
        await self._save_checkout(payment_provider.name, checkout)
        return checkout

    async def get_checkout(
        self,
        checkout_id: str,
        provider: str | None = None,
    ) -> PaymentCheckout:
        payment_provider = self.registry.get(provider)
        checkout = await payment_provider.get_checkout(checkout_id)
        await self._save_checkout(payment_provider.name, checkout)
        return checkout

    async def get_payment_status(
        self,
        checkout_id: str,
        provider: str | None = None,
    ) -> str:
        payment_provider = self.registry.get(provider)
        return await payment_provider.get_payment_status(checkout_id)

    async def create_refund(
        self,
        checkout_id: str,
        amount: int,
        currency: str,
        reference: str | None = None,
        provider: str | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentRefund:
        payment_provider = self.registry.get(provider)
        refund = await payment_provider.create_refund(
            checkout_id=checkout_id,
            amount=amount,
            currency=currency,
            reference=reference,
            provider_options=provider_options,
        )
        await self._save_refund(payment_provider.name, refund)
        return refund

    async def _save_checkout(self, provider: str, checkout: PaymentCheckout) -> None:
        if self.store is not None:
            await self.store.save_checkout(provider, checkout)

    async def _save_refund(self, provider: str, refund: PaymentRefund) -> None:
        if self.store is not None:
            await self.store.save_refund(provider, refund)
