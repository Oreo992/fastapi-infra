import importlib.util

import pytest

from infra.config.models import InfraSettings
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.manager import PluginDependencyError, PluginManager


def test_builtin_plugins_include_optional_backend_plugin_names():
    names = [plugin.metadata.name for plugin in get_builtin_plugins()]

    assert "database" in names
    assert "cache" in names
    assert "http" in names


@pytest.mark.asyncio
async def test_backend_plugins_are_disabled_by_default():
    manager = PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins())

    await manager.startup()
    await manager.shutdown()

    assert manager.get("database", default=None) is None
    assert manager.get("cache", default=None) is None
    assert manager.get("http", default=None) is None


@pytest.mark.asyncio
async def test_enabled_backend_plugins_register_fake_services(monkeypatch):
    from infra.plugins.cache import plugin as cache_plugin
    from infra.plugins.database import plugin as database_plugin
    from infra.plugins.http import plugin as http_plugin

    class FakeDatabaseManager:
        def __init__(self, config):
            self.config = config
            self.initialized = False
            self.closed = False

        async def initialize(self):
            self.initialized = True

        async def health_check(self):
            return True

        async def close(self):
            self.closed = True

    class FakeCacheDatabaseManager:
        def __init__(self, config=None):
            self.config = config or {}
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeCacheService:
        def __init__(self, namespace="", db_manager=None):
            self.namespace = namespace
            self._db_manager = db_manager

    class FakeHttpClient:
        def __init__(
            self,
            base_url="",
            timeout=30.0,
            headers=None,
            instrumentation=None,
            propagate_trace_headers=True,
        ):
            self.base_url = base_url
            self.timeout = timeout
            self.headers = headers or {}
            self.instrumentation = instrumentation
            self.propagate_trace_headers = propagate_trace_headers
            self.closed = False

        async def close(self):
            self.closed = True

    def fake_find_spec(name):
        if name in {"aiomysql", "redis", "aiohttp", "orjson"}:
            return object()
        return importlib.util.find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(
        database_plugin,
        "_load_database_manager",
        lambda: FakeDatabaseManager,
    )
    monkeypatch.setattr(
        cache_plugin,
        "_load_database_manager",
        lambda: FakeCacheDatabaseManager,
    )
    monkeypatch.setattr(cache_plugin, "_load_cache_service", lambda: FakeCacheService)
    monkeypatch.setattr(http_plugin, "_load_http_client", lambda: FakeHttpClient)

    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "config": {"mysql_host": "db.internal"},
                        "connect_on_startup": True,
                    },
                },
                "cache": {
                    "enabled": True,
                    "config": {"default_provider": "redis", "namespace": "tenant-a"},
                },
                "http": {
                    "enabled": True,
                    "config": {
                        "default_provider": "aiohttp",
                        "base_url": "https://api.example.test",
                        "timeout": 3.5,
                        "headers": {"X-Test": "yes"},
                    },
                },
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=get_builtin_plugins())

    await manager.startup()

    database = manager.get("database")
    cache = manager.get("cache")
    http = manager.get("http")

    assert isinstance(database, FakeDatabaseManager)
    assert database.config == {"mysql_host": "db.internal"}
    assert database.initialized is True
    assert isinstance(cache, FakeCacheService)
    assert cache.namespace == "tenant-a"
    assert cache._db_manager is database
    assert isinstance(http, FakeHttpClient)
    assert http.base_url == "https://api.example.test"
    assert http.timeout == 3.5
    assert http.headers == {"X-Test": "yes"}
    assert http.instrumentation is None
    assert http.propagate_trace_headers is True

    await manager.shutdown()

    assert database.closed is True
    assert http.closed is True


@pytest.mark.asyncio
async def test_http_plugin_uses_observability_service_when_available(monkeypatch):
    from infra.plugins.http import plugin as http_plugin

    class FakeHttpClient:
        def __init__(
            self,
            base_url="",
            timeout=30.0,
            headers=None,
            instrumentation=None,
            propagate_trace_headers=True,
        ):
            self.instrumentation = instrumentation
            self.propagate_trace_headers = propagate_trace_headers
            self.closed = False

        async def close(self):
            self.closed = True

    def fake_find_spec(name):
        if name in {"aiohttp", "orjson"}:
            return object()
        return importlib.util.find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(http_plugin, "_load_http_client", lambda: FakeHttpClient)

    settings = InfraSettings(
        infra={
            "plugins": {
                "observability": {"enabled": True},
                "http": {
                    "enabled": True,
                    "config": {
                        "default_provider": "aiohttp",
                        "propagate_trace_headers": False,
                    },
                },
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=get_builtin_plugins())

    await manager.startup()

    http = manager.get("http")
    observability = manager.get("observability")
    assert isinstance(http, FakeHttpClient)
    assert http.instrumentation is observability
    assert http.propagate_trace_headers is False

    await manager.shutdown()


@pytest.mark.asyncio
async def test_http_plugin_uses_mock_provider_without_aiohttp(monkeypatch):
    from infra.plugins.http import plugin as http_plugin

    class FakeMockHttpClient:
        def __init__(self, base_url="", status_code=200, body=None, headers=None):
            self.base_url = base_url
            self.status_code = status_code
            self.body = body
            self.headers = headers
            self.closed = False

        async def close(self):
            self.closed = True

    def fail_if_aiohttp_client_imports():
        pytest.fail("aiohttp client should not be imported for mock provider")

    monkeypatch.setattr(http_plugin, "_load_http_client", fail_if_aiohttp_client_imports)
    monkeypatch.setattr(http_plugin, "_load_mock_http_client", lambda: FakeMockHttpClient)

    manager = PluginManager(
        settings=InfraSettings(
            infra={
                "plugins": {
                    "http": {
                        "enabled": True,
                        "config": {
                            "base_url": "mock://local",
                            "mock_status_code": 202,
                            "mock_body": {"accepted": True},
                        },
                    }
                }
            }
        ),
        plugins=[http_plugin.HTTPPlugin()],
    )

    await manager.startup()

    http = manager.get("http")
    assert isinstance(http, FakeMockHttpClient)
    assert http.base_url == "mock://local"
    assert http.status_code == 202
    assert http.body == {"accepted": True}

    await manager.shutdown()

    assert http.closed is True


@pytest.mark.asyncio
async def test_cache_plugin_creates_and_closes_owned_database_when_service_missing(monkeypatch):
    from infra.plugins.cache import plugin as cache_plugin

    owned_managers = []

    class FakeDatabaseManager:
        def __init__(self, config):
            self.config = config
            self.closed = False
            owned_managers.append(self)

        async def close(self):
            self.closed = True

    class FakeCacheService:
        def __init__(self, namespace="", db_manager=None):
            self.namespace = namespace
            self._db_manager = db_manager

    def fake_find_spec(name):
        if name in {"aiomysql", "redis", "orjson"}:
            return object()
        return importlib.util.find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(
        cache_plugin,
        "_load_database_manager",
        lambda: FakeDatabaseManager,
    )
    monkeypatch.setattr(cache_plugin, "_load_cache_service", lambda: FakeCacheService)

    settings = InfraSettings(
        infra={
            "plugins": {
                "cache": {
                    "enabled": True,
                    "config": {
                        "namespace": "tenant-b",
                        "default_provider": "redis",
                        "database_config": {"redis_url": "redis://cache-only/0"},
                    },
                },
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[cache_plugin.CachePlugin()])

    await manager.startup()

    cache = manager.get("cache")

    assert isinstance(cache, FakeCacheService)
    assert cache.namespace == "tenant-b"
    assert cache._db_manager is owned_managers[0]
    assert owned_managers[0].config == {"redis_url": "redis://cache-only/0"}

    await manager.shutdown()

    assert owned_managers[0].closed is True


@pytest.mark.asyncio
async def test_cache_plugin_does_not_close_shared_database_service(monkeypatch):
    from infra.plugins.cache import plugin as cache_plugin
    from infra.plugins.database import plugin as database_plugin

    class FakeDatabaseManager:
        def __init__(self, config):
            self.config = config
            self.close_calls = 0

        async def health_check(self):
            return True

        async def close(self):
            self.close_calls += 1

    class FakeCacheService:
        def __init__(self, namespace="", db_manager=None):
            self.namespace = namespace
            self._db_manager = db_manager

    def fake_find_spec(name):
        if name in {"aiomysql", "redis", "orjson"}:
            return object()
        return importlib.util.find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(
        database_plugin,
        "_load_database_manager",
        lambda: FakeDatabaseManager,
    )
    monkeypatch.setattr(cache_plugin, "_load_cache_service", lambda: FakeCacheService)

    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {"enabled": True, "config": {"default_provider": "connections"}},
                "cache": {"enabled": True, "config": {"default_provider": "redis"}},
            }
        }
    )
    manager = PluginManager(
        settings=settings,
        plugins=[database_plugin.DatabasePlugin(), cache_plugin.CachePlugin()],
    )

    await manager.startup()

    database = manager.get("database")
    cache = manager.get("cache")

    assert cache._db_manager is database

    await manager.plugins["cache"].shutdown(manager._contexts["cache"])

    assert database.close_calls == 0

    await manager.shutdown()

    assert database.close_calls == 1


@pytest.mark.asyncio
async def test_database_plugin_reports_unhealthy_when_health_check_fails(monkeypatch):
    from infra.plugins.database import plugin as database_plugin

    class FakeDatabaseManager:
        def __init__(self, config):
            self.config = config

        async def health_check(self):
            return False

        async def close(self):
            return None

    def fake_find_spec(name):
        if name in {"aiomysql", "redis"}:
            return object()
        return importlib.util.find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(
        database_plugin,
        "_load_database_manager",
        lambda: FakeDatabaseManager,
    )
    manager = PluginManager(
        settings=InfraSettings(
            infra={
                "plugins": {
                    "database": {"enabled": True, "config": {"default_provider": "connections"}}
                }
            }
        ),
        plugins=[database_plugin.DatabasePlugin()],
    )

    with pytest.raises(PluginDependencyError, match="plugin is unhealthy: database"):
        await manager.startup()

    assert manager.get("database") is None
    status = manager.health.snapshot()["database"]
    assert status.status.value == "unhealthy"
    assert status.message == "database health check failed"


@pytest.mark.asyncio
async def test_database_plugin_uses_memory_provider_without_connection_imports(monkeypatch):
    from infra.plugins.database import plugin as database_plugin

    class FakeMemoryDatabaseManager:
        def __init__(self, config):
            self.config = config
            self.closed = False

        async def health_check(self):
            return True

        async def close(self):
            self.closed = True

    def fail_if_connection_manager_imports():
        pytest.fail("connection database manager should not be imported for memory provider")

    monkeypatch.setattr(
        database_plugin, "_load_database_manager", fail_if_connection_manager_imports
    )
    monkeypatch.setattr(
        database_plugin,
        "_load_memory_database_manager",
        lambda: FakeMemoryDatabaseManager,
    )
    manager = PluginManager(
        settings=InfraSettings(
            infra={
                "plugins": {
                    "database": {
                        "enabled": True,
                        "config": {
                            "config": {"mysql_enabled": False, "redis_enabled": False},
                        },
                    }
                }
            }
        ),
        plugins=[database_plugin.DatabasePlugin()],
    )

    await manager.startup()

    database = manager.get("database")
    assert isinstance(database, FakeMemoryDatabaseManager)
    assert database.config == {"mysql_enabled": False, "redis_enabled": False}

    await manager.shutdown()

    assert database.closed is True


@pytest.mark.asyncio
async def test_cache_plugin_reports_unhealthy_when_cache_health_check_fails(monkeypatch):
    from infra.plugins.cache import plugin as cache_plugin

    class FakeDatabaseManager:
        def __init__(self, config=None):
            self.config = config or {}
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeCacheService:
        def __init__(self, namespace="", db_manager=None):
            self.namespace = namespace
            self._db_manager = db_manager

        async def health_check(self):
            return False

    def fake_find_spec(name):
        if name in {"redis", "orjson"}:
            return object()
        return importlib.util.find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(
        cache_plugin,
        "_load_database_manager",
        lambda: FakeDatabaseManager,
    )
    monkeypatch.setattr(cache_plugin, "_load_cache_service", lambda: FakeCacheService)

    manager = PluginManager(
        settings=InfraSettings(
            infra={"plugins": {"cache": {"enabled": True, "config": {"default_provider": "redis"}}}}
        ),
        plugins=[cache_plugin.CachePlugin()],
    )

    with pytest.raises(PluginDependencyError, match="plugin is unhealthy: cache"):
        await manager.startup()

    assert manager.get("cache") is None
    status = manager.health.snapshot()["cache"]
    assert status.status.value == "unhealthy"
    assert status.message == "cache health check failed"


@pytest.mark.asyncio
async def test_enabled_cache_plugin_uses_memory_provider_without_redis(monkeypatch):
    from infra.plugins.cache import plugin as cache_plugin

    assert cache_plugin.CachePlugin.metadata.optional_dependencies == []

    class FakeMemoryCacheService:
        def __init__(self, namespace=""):
            self.namespace = namespace

        async def health_check(self):
            return True

    def fail_if_cache_service_imports():
        pytest.fail("redis cache service should not be imported for memory provider")

    monkeypatch.setattr(cache_plugin, "_load_cache_service", fail_if_cache_service_imports)
    monkeypatch.setattr(
        cache_plugin,
        "_load_memory_cache_service",
        lambda: FakeMemoryCacheService,
    )

    settings = InfraSettings(
        infra={"plugins": {"cache": {"enabled": True, "config": {"namespace": "local"}}}}
    )
    manager = PluginManager(settings=settings, plugins=[cache_plugin.CachePlugin()])

    await manager.startup()

    cache = manager.get("cache")
    assert isinstance(cache, FakeMemoryCacheService)
    assert cache.namespace == "local"

    await manager.shutdown()


def test_cache_service_requires_explicit_database_manager():
    from infra.cache.service import CacheService

    with pytest.raises(TypeError):
        CacheService()
