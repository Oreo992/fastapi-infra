import pytest

from infra.database import manager as database_manager


class FakeRedisClient:
    def __init__(self) -> None:
        self.closed = False
        self.ping_calls = 0

    async def ping(self) -> bool:
        self.ping_calls += 1
        return True

    async def aclose(self) -> None:
        self.closed = True


class FailingCloseRedisClient(FakeRedisClient):
    async def aclose(self) -> None:
        raise RuntimeError("redis close failed")


class FakeRedisModule:
    def __init__(self) -> None:
        self.client = FakeRedisClient()
        self.urls: list[str] = []

    def from_url(self, url: str, **kwargs):
        self.urls.append(url)
        return self.client


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, sql: str, params=None) -> None:
        self.statements.append(sql)

    async def fetchone(self):
        return {"1": 1}


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()
        self.commits = 0

    def cursor(self, cursor_type=None):
        return self.cursor_obj

    async def ping(self, reconnect: bool = True) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class FakeAcquire:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.conn = FakeConnection()
        self.minsize = 1
        self.maxsize = 1
        self.size = 1
        self.freesize = 1
        self.closed = False

    def acquire(self):
        return FakeAcquire(self.conn)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class FakeAiomysqlModule:
    DictCursor = object

    def __init__(self) -> None:
        self.pool = FakePool()
        self.create_pool_calls: list[dict[str, object]] = []

    async def create_pool(self, **kwargs):
        self.create_pool_calls.append(kwargs)
        return self.pool


@pytest.mark.asyncio
async def test_database_manager_can_initialize_redis_without_mysql(monkeypatch):
    redis_module = FakeRedisModule()
    monkeypatch.setattr(
        database_manager,
        "_load_aiomysql",
        lambda: pytest.fail("mysql should not be loaded"),
    )
    monkeypatch.setattr(database_manager, "_load_redis", lambda: redis_module)
    manager = database_manager.DatabaseManager(
        {"mysql_enabled": False, "redis_url": "redis://cache-only/0"}
    )

    await manager.initialize()

    assert await manager.health_check() is True
    assert redis_module.urls == ["redis://cache-only/0"]
    with pytest.raises(RuntimeError, match="MySQL is disabled"):
        async with manager.get_connection():
            pass

    await manager.close()

    assert redis_module.client.closed is True


@pytest.mark.asyncio
async def test_memory_database_manager_stores_documents_and_migration_records():
    from infra.database import MemoryDatabaseManager

    database = MemoryDatabaseManager({"mysql_enabled": False, "redis_enabled": False})

    await database.initialize()
    assert await database.health_check() is True

    stored = await database.put_document("examples", "greeting", {"message": "hello"})
    assert stored == {
        "collection": "examples",
        "key": "greeting",
        "value": {"message": "hello"},
    }
    assert await database.get_document("examples", "greeting") == stored
    assert await database.delete_document("examples", "greeting") is True
    assert await database.get_document("examples", "greeting") is None

    await database.execute_sql(
        "INSERT INTO infra_schema_migrations VALUES (%s, %s, %s, %s)",
        ("20260520000000", "create_table", "checksum", "2026-05-20T00:00:00Z"),
    )
    assert await database.fetch_all("SELECT * FROM infra_schema_migrations") == [
        {
            "version": "20260520000000",
            "name": "create_table",
            "checksum": "checksum",
            "applied_at": "2026-05-20T00:00:00Z",
        }
    ]

    await database.close()
    assert await database.health_check() is False


@pytest.mark.asyncio
async def test_cache_service_health_check_pings_redis(monkeypatch):
    from infra.cache.service import CacheService

    redis_module = FakeRedisModule()
    monkeypatch.setattr(database_manager, "_load_redis", lambda: redis_module)
    manager = database_manager.DatabaseManager(
        {"mysql_enabled": False, "redis_url": "redis://cache-only/0"}
    )
    cache = CacheService(namespace="health", db_manager=manager)

    assert await cache.health_check() is True
    assert redis_module.client.ping_calls == 2

    await manager.close()


@pytest.mark.asyncio
async def test_memory_cache_service_supports_ttl_and_delete(monkeypatch):
    from infra.cache import service as cache_service

    now = 1000.0
    monkeypatch.setattr(cache_service.time, "monotonic", lambda: now)
    cache = cache_service.MemoryCacheService(namespace="local")

    assert await cache.health_check() is True
    assert await cache.set("greeting", {"message": "hello"}, ttl=10) is True
    assert await cache.get("greeting") == {"message": "hello"}
    assert await cache.exists("greeting") is True

    now = 1011.0
    assert await cache.get("greeting") is None
    assert await cache.exists("greeting") is False

    assert await cache.set("delete-me", "value", ttl=10) is True
    assert await cache.delete("delete-me") is True
    assert await cache.delete("delete-me") is False


@pytest.mark.asyncio
async def test_database_manager_can_initialize_mysql_without_redis(monkeypatch):
    aiomysql_module = FakeAiomysqlModule()
    monkeypatch.setattr(database_manager, "_load_aiomysql", lambda: aiomysql_module)
    monkeypatch.setattr(
        database_manager,
        "_load_redis",
        lambda: pytest.fail("redis should not be loaded"),
    )
    manager = database_manager.DatabaseManager(
        {
            "mysql_enabled": True,
            "redis_enabled": False,
            "mysql_host": "db.internal",
            "mysql_pool_minsize": 1,
            "mysql_pool_maxsize": 1,
        }
    )

    await manager.initialize()

    assert await manager.health_check() is True
    assert aiomysql_module.create_pool_calls[0]["host"] == "db.internal"
    with pytest.raises(RuntimeError, match="Redis is disabled"):
        manager.get_redis()

    await manager.close()

    assert aiomysql_module.pool.closed is True


@pytest.mark.asyncio
async def test_database_manager_close_preserves_failed_redis_client_for_retry():
    manager = database_manager.DatabaseManager({"mysql_enabled": False})
    client = FailingCloseRedisClient()
    manager._redis_clients[123] = client
    manager._initialized = True

    with pytest.raises(RuntimeError, match="redis close failed"):
        await manager.close()

    assert manager._redis_clients[123] is client
    assert manager._initialized is True
