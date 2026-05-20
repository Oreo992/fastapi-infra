from typing import Protocol

from infra.plugins.payment.models import PaymentCheckout, PaymentRefund


class PaymentProvider(Protocol):
    name: str

    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentCheckout:
        raise NotImplementedError

    async def get_checkout(self, checkout_id: str) -> PaymentCheckout:
        raise NotImplementedError

    async def get_payment_status(self, checkout_id: str) -> str:
        raise NotImplementedError

    async def create_refund(
        self,
        checkout_id: str,
        amount: int,
        currency: str,
        reference: str | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentRefund:
        raise NotImplementedError
