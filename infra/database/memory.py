from __future__ import annotations

from typing import Any


class MemoryDatabaseManager:
    """In-process database service for local development and generated tests."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._documents: dict[tuple[str, str], Any] = {}
        self._migration_records: list[dict[str, Any]] = []
        self.closed = False
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.initialized = False

    async def health_check(self) -> bool:
        return not self.closed

    async def put_document(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        self._documents[(collection, key)] = dict(value)
        return {"collection": collection, "key": key, "value": dict(value)}

    async def get_document(self, collection: str, key: str) -> dict[str, Any] | None:
        value = self._documents.get((collection, key))
        if value is None:
            return None
        return {"collection": collection, "key": key, "value": dict(value)}

    async def delete_document(self, collection: str, key: str) -> bool:
        return self._documents.pop((collection, key), None) is not None

    async def execute_sql(self, sql: str, params: Any = None, commit: bool = True) -> int:
        normalized = " ".join(sql.split()).lower()
        if "insert into infra_schema_migrations" in normalized and params is not None:
            version, name, checksum, applied_at = params
            self._migration_records.append(
                {
                    "version": version,
                    "name": name,
                    "checksum": checksum,
                    "applied_at": applied_at,
                }
            )
            return 1
        return 0

    async def fetch_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        normalized = " ".join(sql.split()).lower()
        if "from infra_schema_migrations" in normalized:
            return list(self._migration_records)
        return []

    async def fetch_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None
