import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.discovery import get_available_plugins, load_entry_point_plugins
from infra.plugins.manager import PluginDependencyError, PluginManager


class EntryPointPlugin:
    metadata = PluginMetadata(
        name="external",
        version="1.0.0",
        default_enabled=True,
        provides=["external"],
    )
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["external"] = "loaded"

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("external", HealthState.HEALTHY)


class FakeEntryPoint:
    def __init__(self, loaded, name="external"):
        self.name = name
        self._loaded = loaded
        self.loaded = False

    def load(self):
        self.loaded = True
        return self._loaded


def test_load_entry_point_plugins_accepts_plugin_class():
    plugins = load_entry_point_plugins(
        entry_points_loader=lambda group: [FakeEntryPoint(EntryPointPlugin)]
    )

    assert [plugin.metadata.name for plugin in plugins] == ["external"]
    assert isinstance(plugins[0], EntryPointPlugin)


def test_plugins_package_exports_builtin_plugin_factory():
    from infra.plugins import get_builtin_plugins

    assert "ai" in {plugin.metadata.name for plugin in get_builtin_plugins()}


@pytest.mark.asyncio
async def test_discovered_entry_point_plugin_can_start_through_manager():
    plugins = load_entry_point_plugins(
        entry_points_loader=lambda group: [FakeEntryPoint(EntryPointPlugin)]
    )
    manager = PluginManager(
        settings=InfraSettings(infra={"plugins": {"external": {"enabled": True}}}),
        plugins=plugins,
    )

    await manager.startup()

    assert manager.get("external") == "loaded"
    assert manager.health.snapshot()["external"].status is HealthState.HEALTHY


def test_load_entry_point_plugins_rejects_invalid_plugin_shape():
    with pytest.raises(PluginDependencyError, match="does not implement InfraPlugin"):
        load_entry_point_plugins(entry_points_loader=lambda group: [FakeEntryPoint(object)])


def test_load_entry_point_plugins_skips_unconfigured_entry_points_without_loading():
    external = FakeEntryPoint(EntryPointPlugin)
    invalid = FakeEntryPoint(object, name="unused")

    plugins = load_entry_point_plugins(
        configured_names={"external"},
        entry_points_loader=lambda group: [external, invalid],
    )

    assert [plugin.metadata.name for plugin in plugins] == ["external"]
    assert external.loaded is True
    assert invalid.loaded is False


def test_get_available_plugins_only_loads_configured_external_entry_points(monkeypatch):
    external = FakeEntryPoint(EntryPointPlugin)
    unused = FakeEntryPoint(object, name="unused")

    monkeypatch.setattr(
        "infra.plugins.discovery.entry_points",
        lambda group: [external, unused],
    )

    plugins = get_available_plugins(
        InfraSettings(infra={"plugins": {"external": {"enabled": True}}})
    )

    assert "external" in {plugin.metadata.name for plugin in plugins}
    assert external.loaded is True
    assert unused.loaded is False


def test_load_entry_point_plugins_requires_entry_point_name_to_match_plugin_name():
    with pytest.raises(PluginDependencyError, match="returned plugin"):
        load_entry_point_plugins(
            entry_points_loader=lambda group: [FakeEntryPoint(EntryPointPlugin, name="wrong")]
        )
