import pytest

from infra.config.models import InfraSettings
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.manager import PluginManager


def test_builtin_plugins_include_first_batch_plugin_names():
    names = [plugin.metadata.name for plugin in get_builtin_plugins()]

    assert names == [
        "ai",
        "auth",
        "observability",
        "tasks",
        "storage",
        "webhooks",
        "payment",
        "ratelimit",
        "notifications",
    ]


@pytest.mark.asyncio
async def test_builtin_plugins_start_memory_safe_services_by_default():
    manager = PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins())

    await manager.startup()
    await manager.shutdown()

    for service_name in [
        "ai",
        "auth",
        "observability",
        "tasks",
        "storage",
        "webhooks",
        "payment",
        "ratelimit",
        "notifications",
    ]:
        assert manager.get(service_name) is not None


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
    assert manager.get("ai") is not None
