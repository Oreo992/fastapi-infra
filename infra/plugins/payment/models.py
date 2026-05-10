from pydantic import BaseModel


class PaymentCheckout(BaseModel):
    id: str
    amount: int
    currency: str
    reference: str | None = None
    status: str
    url: str
