from collections.abc import Iterable
from typing import Any, cast

import pytest

import infra.database.repository as repository_module
from infra.database.repository import BaseRepository


class FakeCursor:
    def __init__(self, rows: Iterable[Any]) -> None:
        self._rows = list(rows)
        self.executed: list[tuple[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, params))

    async def fetchone(self):
        if not self._rows:
            return None
        return self._rows.pop(0)


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self, *args, **kwargs) -> FakeCursor:
        return self._cursor


class FakeConnectionContext:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeDatabase:
    def __init__(self, cursor: FakeCursor) -> None:
        self._connection = FakeConnection(cursor)

    async def initialize(self) -> None:
        return None

    def get_connection(self) -> FakeConnectionContext:
        return FakeConnectionContext(self._connection)


def _repository(cursor: FakeCursor) -> BaseRepository[Any]:
    return BaseRepository("items", db=cast(Any, FakeDatabase(cursor)))


@pytest.mark.asyncio
async def test_find_one_by_uses_single_limit_query(monkeypatch) -> None:
    monkeypatch.setattr(repository_module, "_dict_cursor", lambda: object())
    cursor = FakeCursor([{"id": "item-1", "name": "Item", "is_active": 1}])

    result = await _repository(cursor).find_one_by({"name": "Item"})

    assert result == {"id": "item-1", "name": "Item", "is_active": 1}
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "SELECT * FROM items" in sql
    assert "COUNT" not in sql
    assert "LIMIT 1" in sql
    assert params == ["Item", 1]


@pytest.mark.asyncio
async def test_exists_uses_select_one_without_loading_row() -> None:
    cursor = FakeCursor([(1,)])

    assert await _repository(cursor).exists("item-1") is True

    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "SELECT 1 FROM items" in sql
    assert "SELECT *" not in sql
    assert "LIMIT 1" in sql
    assert params == ["item-1", 1]
