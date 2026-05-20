import json
import re
from typing import Any, Protocol

from infra.plugins.webhooks.models import WebhookEvent

_TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
WEBHOOK_STORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS infra_webhook_events (
    provider VARCHAR(64) NOT NULL,
    event_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(255) NOT NULL,
    payload_json JSON NOT NULL,
    headers_json JSON NOT NULL,
    received_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (provider, event_id)
);
""".strip()


class WebhookStore(Protocol):
    async def record_once(self, event: WebhookEvent) -> bool:
        raise NotImplementedError


class InMemoryWebhookStore:
    def __init__(self) -> None:
        self._event_keys: set[tuple[str, str]] = set()

    async def record_once(self, event: WebhookEvent) -> bool:
        key = (event.provider, event.id)
        if key in self._event_keys:
            return False
        self._event_keys.add(key)
        return True


class SqlWebhookStore:
    def __init__(self, database: Any, table_name: str = "infra_webhook_events") -> None:
        if not _TABLE_NAME_RE.fullmatch(table_name):
            raise ValueError("webhook table_name must be a simple SQL identifier")
        self.database = database
        self.table_name = table_name
        self._table_ready = False

    async def record_once(self, event: WebhookEvent) -> bool:
        await self.ensure_table()
        try:
            await self.database.execute_sql(
                f"""
                INSERT INTO {self.table_name}
                    (provider, event_id, event_type, payload_json, headers_json, received_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    event.provider,
                    event.id,
                    event.type,
                    json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(event.headers, ensure_ascii=False, separators=(",", ":")),
                    event.received_at.isoformat(),
                ),
            )
        except Exception as exc:
            if _is_duplicate_key_error(exc):
                return False
            raise
        return True

    async def ensure_table(self) -> None:
        if self._table_ready:
            return
        await self.database.execute_sql(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                provider VARCHAR(64) NOT NULL,
                event_id VARCHAR(255) NOT NULL,
                event_type VARCHAR(255) NOT NULL,
                payload_json JSON NOT NULL,
                headers_json JSON NOT NULL,
                received_at VARCHAR(64) NOT NULL,
                PRIMARY KEY (provider, event_id)
            )
            """)
        self._table_ready = True


def _is_duplicate_key_error(exc: Exception) -> bool:
    codes = {1062, 23505, 2067}
    for arg in getattr(exc, "args", ()):
        if arg in codes:
            return True
        if isinstance(arg, str) and any(str(code) in arg for code in codes):
            return True
    message = str(exc).lower()
    return "duplicate" in message or "unique constraint" in message
