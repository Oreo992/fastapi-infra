import importlib.util

import pytest

from infra.config.models import InfraSettings
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.manager import PluginManager


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

    class FakeCacheService:
        def __init__(self, namespace=""):
            self.namespace = namespace

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
    assert http.closed is True
