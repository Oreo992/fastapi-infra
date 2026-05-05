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

        async def close(self):
            self.closed = True

    class FakeCacheDatabaseManager:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeCacheService:
        def __init__(self, namespace=""):
            self.namespace = namespace
            self._db_manager = FakeCacheDatabaseManager()

    class FakeHttpClient:
        def __init__(self, base_url="", timeout=30.0, headers=None):
            self.base_url = base_url
            self.timeout = timeout
            self.headers = headers or {}
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
    monkeypatch.setattr(cache_plugin, "_load_cache_service", lambda: FakeCacheService)
    monkeypatch.setattr(http_plugin, "_load_http_client", lambda: FakeHttpClient)

    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "config": {"mysql_host": "db.internal"},
                        "connect_on_startup": True,
                    },
                },
                "cache": {
                    "enabled": True,
                    "config": {"namespace": "tenant-a"},
                },
                "http": {
                    "enabled": True,
                    "config": {
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
    assert isinstance(http, FakeHttpClient)
    assert http.base_url == "https://api.example.test"
    assert http.timeout == 3.5
    assert http.headers == {"X-Test": "yes"}

    await manager.shutdown()

    assert database.closed is True
    assert cache._db_manager.closed is True
    assert http.closed is True


@pytest.mark.asyncio
async def test_enabled_cache_plugin_requires_service_import_dependencies(monkeypatch):
    from infra.plugins.cache import plugin as cache_plugin

    expected_dependencies = {"orjson", "aiomysql", "redis"}
    assert set(cache_plugin.CachePlugin.metadata.optional_dependencies) == expected_dependencies

    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name):
        if name in expected_dependencies:
            return None
        return original_find_spec(name)

    def fail_if_cache_service_imports():
        pytest.fail("cache service should not be imported when dependencies are missing")

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(cache_plugin, "_load_cache_service", fail_if_cache_service_imports)

    settings = InfraSettings(infra={"plugins": {"cache": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[cache_plugin.CachePlugin()])

    with pytest.raises(PluginDependencyError, match="missing optional dependency") as exc:
        await manager.startup()

    for dependency in expected_dependencies:
        assert dependency in str(exc.value)
