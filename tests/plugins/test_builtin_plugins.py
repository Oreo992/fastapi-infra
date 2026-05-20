import pytest

from infra.config.models import InfraSettings
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.manager import PluginManager
from infra.scaffold import BUILTIN_PLUGIN_NAMES


def test_builtin_plugins_include_first_batch_plugin_names():
    names = [plugin.metadata.name for plugin in get_builtin_plugins()]

    assert names == [
        "ai",
        "speech",
        "auth",
        "database",
        "cache",
        "observability",
        "http",
        "tasks",
        "storage",
        "webhooks",
        "payment",
        "ratelimit",
        "notifications",
    ]


def test_scaffold_builtin_plugin_names_follow_builtin_registry():
    assert BUILTIN_PLUGIN_NAMES == tuple(plugin.metadata.name for plugin in get_builtin_plugins())


@pytest.mark.asyncio
async def test_builtin_plugins_are_core_minimal_by_default():
    manager = PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins())

    await manager.startup()
    await manager.shutdown()

    for service_name in [
        "ai",
        "speech",
        "auth",
        "observability",
        "tasks",
        "storage",
        "webhooks",
        "payment",
        "ratelimit",
        "notifications",
    ]:
        assert manager.get(service_name) is None


@pytest.mark.asyncio
async def test_builtin_plugins_can_be_disabled_by_feature_flag():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {"enabled": False},
                "notifications": {"enabled": False},
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=get_builtin_plugins())

    await manager.startup()

    assert manager.get("payment", default=None) is None
    assert manager.get("notifications", default=None) is None
    assert manager.get("ai") is None
