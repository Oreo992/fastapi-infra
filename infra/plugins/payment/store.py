import re
from typing import Any, Protocol

from infra.plugins.payment.models import PaymentCheckout, PaymentRefund

_TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
PAYMENT_STORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS infra_payment_checkouts (
    provider VARCHAR(64) NOT NULL,
    checkout_id VARCHAR(255) NOT NULL,
    amount BIGINT NOT NULL,
    currency VARCHAR(16) NOT NULL,
    reference VARCHAR(255),
    status VARCHAR(64) NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (provider, checkout_id)
);

CREATE TABLE IF NOT EXISTS infra_payment_refunds (
    provider VARCHAR(64) NOT NULL,
    refund_id VARCHAR(255) NOT NULL,
    checkout_id VARCHAR(255) NOT NULL,
    amount BIGINT NOT NULL,
    currency VARCHAR(16) NOT NULL,
    reference VARCHAR(255),
    status VARCHAR(64) NOT NULL,
    PRIMARY KEY (provider, refund_id)
);
""".strip()


class PaymentStore(Protocol):
    async def save_checkout(self, provider: str, checkout: PaymentCheckout) -> None:
        raise NotImplementedError

    async def save_refund(self, provider: str, refund: PaymentRefund) -> None:
        raise NotImplementedError


class InMemoryPaymentStore:
    def __init__(self) -> None:
        self.checkouts: dict[tuple[str, str], PaymentCheckout] = {}
        self.refunds: dict[tuple[str, str], PaymentRefund] = {}

    async def save_checkout(self, provider: str, checkout: PaymentCheckout) -> None:
        self.checkouts[(provider, checkout.id)] = checkout

    async def save_refund(self, provider: str, refund: PaymentRefund) -> None:
        self.refunds[(provider, refund.id)] = refund


class SqlPaymentStore:
    def __init__(
        self,
        database: Any,
        *,
        checkout_table: str = "infra_payment_checkouts",
        refund_table: str = "infra_payment_refunds",
    ) -> None:
        _validate_table_name(checkout_table, "checkout_table")
        _validate_table_name(refund_table, "refund_table")
        self.database = database
        self.checkout_table = checkout_table
        self.refund_table = refund_table
        self._tables_ready = False

    async def save_checkout(self, provider: str, checkout: PaymentCheckout) -> None:
        await self.ensure_tables()
        await self.database.execute_sql(
            f"""
            INSERT INTO {self.checkout_table}
                (provider, checkout_id, amount, currency, reference, status, url)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                amount = VALUES(amount),
                currency = VALUES(currency),
                reference = VALUES(reference),
                status = VALUES(status),
                url = VALUES(url)
            """,
            (
                provider,
                checkout.id,
                checkout.amount,
                checkout.currency,
                checkout.reference,
                checkout.status,
                checkout.url,
            ),
        )

    async def save_refund(self, provider: str, refund: PaymentRefund) -> None:
        await self.ensure_tables()
        await self.database.execute_sql(
            f"""
            INSERT INTO {self.refund_table}
                (provider, refund_id, checkout_id, amount, currency, reference, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                checkout_id = VALUES(checkout_id),
                amount = VALUES(amount),
                currency = VALUES(currency),
                reference = VALUES(reference),
                status = VALUES(status)
            """,
            (
                provider,
                refund.id,
                refund.checkout_id,
                refund.amount,
                refund.currency,
                refund.reference,
                refund.status,
            ),
        )

    async def ensure_tables(self) -> None:
        if self._tables_ready:
            return
        await self.database.execute_sql(f"""
            CREATE TABLE IF NOT EXISTS {self.checkout_table} (
                provider VARCHAR(64) NOT NULL,
                checkout_id VARCHAR(255) NOT NULL,
                amount BIGINT NOT NULL,
                currency VARCHAR(16) NOT NULL,
                reference VARCHAR(255),
                status VARCHAR(64) NOT NULL,
                url TEXT NOT NULL,
                PRIMARY KEY (provider, checkout_id)
            )
            """)
        await self.database.execute_sql(f"""
            CREATE TABLE IF NOT EXISTS {self.refund_table} (
                provider VARCHAR(64) NOT NULL,
                refund_id VARCHAR(255) NOT NULL,
                checkout_id VARCHAR(255) NOT NULL,
                amount BIGINT NOT NULL,
                currency VARCHAR(16) NOT NULL,
                reference VARCHAR(255),
                status VARCHAR(64) NOT NULL,
                PRIMARY KEY (provider, refund_id)
            )
            """)
        self._tables_ready = True


def _validate_table_name(name: str, field_name: str) -> None:
    if not _TABLE_NAME_RE.fullmatch(name):
        raise ValueError(f"{field_name} must be a simple SQL identifier")
