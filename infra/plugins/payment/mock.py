from infra.plugins.payment.models import PaymentCheckout


class MockPaymentProvider:
    name = "mock"

    def __init__(self) -> None:
        self._next_id = 1
        self._statuses: dict[str, str] = {}

    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
    ) -> PaymentCheckout:
        checkout_id = f"chk_mock_{self._next_id:06d}"
        self._next_id += 1
        status = "pending"
        self._statuses[checkout_id] = status
        return PaymentCheckout(
            id=checkout_id,
            amount=amount,
            currency=currency.upper(),
            reference=reference,
            status=status,
            url=f"mock://checkout/{checkout_id}",
        )

    async def get_payment_status(self, checkout_id: str) -> str:
        return self._statuses.get(checkout_id, "unknown")
