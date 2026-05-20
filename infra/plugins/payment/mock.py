from infra.plugins.payment.models import PaymentCheckout, PaymentRefund


class MockPaymentProvider:
    name = "mock"

    def __init__(self) -> None:
        self._next_id = 1
        self._next_refund_id = 1
        self._checkouts: dict[str, PaymentCheckout] = {}
        self._refunds: dict[str, PaymentRefund] = {}
        self._statuses: dict[str, str] = {}

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
        checkout_id = f"chk_mock_{self._next_id:06d}"
        self._next_id += 1
        status = "pending"
        checkout = PaymentCheckout(
            id=checkout_id,
            amount=amount,
            currency=currency.upper(),
            reference=reference,
            status=status,
            url=f"mock://checkout/{checkout_id}",
        )
        self._checkouts[checkout_id] = checkout
        self._statuses[checkout_id] = status
        return checkout

    async def get_checkout(self, checkout_id: str) -> PaymentCheckout:
        checkout = self._checkouts.get(checkout_id)
        if checkout is not None:
            return checkout
        return PaymentCheckout(
            id=checkout_id,
            amount=0,
            currency="",
            reference=None,
            status=self._statuses.get(checkout_id, "unknown"),
            url=f"mock://checkout/{checkout_id}",
        )

    async def get_payment_status(self, checkout_id: str) -> str:
        return self._statuses.get(checkout_id, "unknown")

    async def create_refund(
        self,
        checkout_id: str,
        amount: int,
        currency: str,
        reference: str | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentRefund:
        if amount <= 0:
            raise ValueError("amount must be positive")

        checkout = self._checkouts.get(checkout_id)
        refund_id = f"rf_mock_{self._next_refund_id:06d}"
        self._next_refund_id += 1
        refund = PaymentRefund(
            id=refund_id,
            checkout_id=checkout_id,
            amount=amount,
            currency=(checkout.currency if checkout is not None else currency).upper(),
            reference=reference,
            status="succeeded" if checkout is not None else "unknown_checkout",
        )
        self._refunds[refund_id] = refund
        if checkout is not None:
            self._statuses[checkout_id] = "refunded"
        return refund
