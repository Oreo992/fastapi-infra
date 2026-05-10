from typing import Protocol

from infra.plugins.payment.models import PaymentCheckout


class PaymentProvider(Protocol):
    name: str

    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
    ) -> PaymentCheckout:
        raise NotImplementedError

    async def get_payment_status(self, checkout_id: str) -> str:
        raise NotImplementedError
