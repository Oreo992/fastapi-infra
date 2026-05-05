import pytest
from pydantic import BaseModel

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.manager import PluginManager


class FakeConfig(BaseModel):
    value: str = "ok"


class FakePlugin:
    metadata = PluginMetadata(name="fake", version="1.0.0", provides=["fake"])
    config_model = FakeConfig

    def __init__(self) -> None:
        self.events: list[str] = []

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        ctx.services["fake"] = self

    async def startup(self, ctx: PluginContext) -> None:
        self.events.append("startup")

    async def shutdown(self, ctx: PluginContext) -> None:
        self.events.append("shutdown")

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("fake", HealthState.HEALTHY)


class DependentPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="dependent",
        version="1.0.0",
        dependencies=["fake"],
        provides=["dependent"],
    )


class MissingDependencyPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="missing",
        version="1.0.0",
        optional_dependencies=["package_that_does_not_exist_fastapi_infra"],
        default_enabled=None,
        provides=["missing"],
    )


@pytest.mark.asyncio
async def test_enabled_plugin_registers_starts_and_stops():
    settings = InfraSettings(infra={"plugins": {"fake": {"enabled": True}}})
    plugin = FakePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()
    await manager.shutdown()

    assert plugin.events == ["register", "startup", "shutdown"]
    assert manager.get("fake") is plugin


@pytest.mark.asyncio
async def test_disabled_plugin_is_not_registered_or_started():
    settings = InfraSettings(infra={"plugins": {"fake": {"enabled": False}}})
    plugin = FakePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    assert plugin.events == []
    assert manager.get("fake", default=None) is None
    assert manager.health.snapshot()["fake"].status is HealthState.DISABLED


@pytest.mark.asyncio
async def test_dependency_order_for_startup_and_reverse_shutdown():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": True},
                "dependent": {"enabled": True},
            }
        }
    )
    fake = FakePlugin()
    dependent = DependentPlugin()
    manager = PluginManager(settings=settings, plugins=[dependent, fake])

    await manager.startup()
    await manager.shutdown()

    assert fake.events == ["register", "startup", "shutdown"]
    assert dependent.events == ["register", "startup", "shutdown"]
    assert list(manager.started_plugins) == ["fake", "dependent"]


@pytest.mark.asyncio
async def test_auto_plugin_skips_when_optional_dependency_is_missing():
    settings = InfraSettings(infra={"plugins": {"missing": {"enabled": None}}})
    plugin = MissingDependencyPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    assert plugin.events == []
    assert manager.health.snapshot()["missing"].status is HealthState.DISABLED


@pytest.mark.asyncio
async def test_forced_plugin_fails_when_optional_dependency_is_missing():
    settings = InfraSettings(infra={"plugins": {"missing": {"enabled": True}}})
    plugin = MissingDependencyPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(Exception, match="missing optional dependency"):
        await manager.startup()
