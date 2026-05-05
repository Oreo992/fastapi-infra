import pytest
from pydantic import BaseModel, ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.manager import PluginDependencyError, PluginManager


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


class DependsOnMissingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="depends_on_missing",
        version="1.0.0",
        dependencies=["missing"],
        provides=["depends_on_missing"],
    )


class DependsOnAbsentPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="depends_on_absent",
        version="1.0.0",
        dependencies=["absent"],
        provides=["depends_on_absent"],
    )


class HealthFailingPlugin(FakePlugin):
    metadata = PluginMetadata(name="health_failing", version="1.0.0")

    async def health_check(self, ctx: PluginContext):
        raise RuntimeError("health failed")


class StartupFailingPlugin(FakePlugin):
    metadata = PluginMetadata(name="startup_failing", version="1.0.0")

    async def startup(self, ctx: PluginContext) -> None:
        self.events.append("startup")
        raise RuntimeError("startup failed")


class RegisterThenStartupFailingPlugin(FakePlugin):
    metadata = PluginMetadata(name="register_then_startup_failing", version="1.0.0")

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        ctx.services["leaky"] = self

    async def startup(self, ctx: PluginContext) -> None:
        self.events.append("startup")
        raise RuntimeError("startup failed")


class UnhealthyPlugin(FakePlugin):
    metadata = PluginMetadata(name="unhealthy", version="1.0.0")

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("unhealthy", HealthState.UNHEALTHY)


class DegradedPlugin(FakePlugin):
    metadata = PluginMetadata(name="degraded", version="1.0.0")

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("degraded", HealthState.DEGRADED)


class ShutdownFailingRollbackPlugin(FakePlugin):
    metadata = PluginMetadata(name="shutdown_failing_rollback", version="1.0.0")

    def __init__(self, attempts: list[str]) -> None:
        super().__init__()
        self.attempts = attempts

    async def shutdown(self, ctx: PluginContext) -> None:
        self.events.append("shutdown")
        self.attempts.append("shutdown_failing_rollback")
        raise RuntimeError("shutdown failed")


class TrackingRollbackPlugin(FakePlugin):
    metadata = PluginMetadata(name="tracking_rollback", version="1.0.0")

    def __init__(self, attempts: list[str]) -> None:
        super().__init__()
        self.attempts = attempts

    async def shutdown(self, ctx: PluginContext) -> None:
        self.events.append("shutdown")
        self.attempts.append("tracking_rollback")


class LaterStartupFailingPlugin(FakePlugin):
    metadata = PluginMetadata(name="later_startup_failing", version="1.0.0")

    async def startup(self, ctx: PluginContext) -> None:
        self.events.append("startup")
        raise RuntimeError("original startup failed")


class StrictConfig(BaseModel):
    value: int


class StrictConfigPlugin(FakePlugin):
    metadata = PluginMetadata(name="strict", version="1.0.0")
    config_model = StrictConfig

    def __init__(self) -> None:
        super().__init__()
        self.config: StrictConfig | None = None

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        self.config = ctx.config


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
    assert list(manager.started_plugins) == ["fake", "dependent"]

    await manager.shutdown()

    assert fake.events == ["register", "startup", "shutdown"]
    assert dependent.events == ["register", "startup", "shutdown"]
    assert list(manager.started_plugins) == []


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

    with pytest.raises(PluginDependencyError, match="missing optional dependency"):
        await manager.startup()


@pytest.mark.asyncio
async def test_health_check_failure_shuts_down_started_plugin():
    settings = InfraSettings(infra={"plugins": {"health_failing": {"enabled": True}}})
    plugin = HealthFailingPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(RuntimeError, match="health failed"):
        await manager.startup()

    assert plugin.events == ["register", "startup", "shutdown"]
    assert list(manager.started_plugins) == []


@pytest.mark.asyncio
async def test_startup_failure_rolls_back_already_started_plugins():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": True},
                "startup_failing": {"enabled": True},
            }
        }
    )
    fake = FakePlugin()
    failing = StartupFailingPlugin()
    manager = PluginManager(settings=settings, plugins=[fake, failing])

    with pytest.raises(RuntimeError, match="startup failed"):
        await manager.startup()

    assert fake.events == ["register", "startup", "shutdown"]
    assert failing.events == ["register", "startup"]


@pytest.mark.asyncio
async def test_current_plugin_startup_failure_does_not_leak_services_or_context():
    settings = InfraSettings(
        infra={"plugins": {"register_then_startup_failing": {"enabled": True}}}
    )
    plugin = RegisterThenStartupFailingPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(RuntimeError, match="startup failed"):
        await manager.startup()

    assert plugin.events == ["register", "startup"]
    assert manager.get("leaky", default=None) is None
    assert "register_then_startup_failing" not in manager._contexts


@pytest.mark.asyncio
async def test_repeated_shutdown_does_not_call_shutdown_twice():
    settings = InfraSettings(infra={"plugins": {"fake": {"enabled": True}}})
    plugin = FakePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()
    await manager.shutdown()
    await manager.shutdown()

    assert plugin.events == ["register", "startup", "shutdown"]
    assert manager.started_plugins == []
    assert manager.active_plugins == set()


@pytest.mark.asyncio
async def test_shutdown_after_startup_rollback_is_noop():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": True},
                "startup_failing": {"enabled": True},
            }
        }
    )
    fake = FakePlugin()
    failing = StartupFailingPlugin()
    manager = PluginManager(settings=settings, plugins=[fake, failing])

    with pytest.raises(RuntimeError, match="startup failed"):
        await manager.startup()
    await manager.shutdown()

    assert fake.events == ["register", "startup", "shutdown"]
    assert failing.events == ["register", "startup"]


@pytest.mark.asyncio
async def test_rollback_preserves_original_error_and_attempts_all_shutdowns():
    settings = InfraSettings(
        infra={
            "plugins": {
                "tracking_rollback": {"enabled": True},
                "shutdown_failing_rollback": {"enabled": True},
                "later_startup_failing": {"enabled": True},
            }
        }
    )
    attempts: list[str] = []
    tracking = TrackingRollbackPlugin(attempts)
    shutdown_failing = ShutdownFailingRollbackPlugin(attempts)
    startup_failing = LaterStartupFailingPlugin()
    manager = PluginManager(
        settings=settings,
        plugins=[tracking, shutdown_failing, startup_failing],
    )

    with pytest.raises(RuntimeError, match="original startup failed"):
        await manager.startup()

    assert attempts == ["shutdown_failing_rollback", "tracking_rollback"]
    assert tracking.events == ["register", "startup", "shutdown"]
    assert shutdown_failing.events == ["register", "startup", "shutdown"]
    assert startup_failing.events == ["register", "startup"]
    assert manager.started_plugins == []


@pytest.mark.asyncio
async def test_forced_optional_dependency_failure_rolls_back_already_started_plugins():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": True},
                "missing": {"enabled": True},
            }
        }
    )
    fake = FakePlugin()
    failing = MissingDependencyPlugin()
    manager = PluginManager(settings=settings, plugins=[fake, failing])

    with pytest.raises(PluginDependencyError, match="missing optional dependency"):
        await manager.startup()

    assert fake.events == ["register", "startup", "shutdown"]
    assert failing.events == []


@pytest.mark.asyncio
async def test_unhealthy_health_result_fails_startup_and_rolls_back():
    settings = InfraSettings(infra={"plugins": {"unhealthy": {"enabled": True}}})
    plugin = UnhealthyPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(PluginDependencyError, match="unhealthy"):
        await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == ["register", "startup", "shutdown"]
    assert snapshot["unhealthy"].status is HealthState.UNHEALTHY
    assert manager.started_plugins == []


@pytest.mark.asyncio
async def test_degraded_health_result_remains_active():
    settings = InfraSettings(infra={"plugins": {"degraded": {"enabled": True}}})
    plugin = DegradedPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == ["register", "startup"]
    assert snapshot["degraded"].status is HealthState.DEGRADED
    assert manager.started_plugins == ["degraded"]


@pytest.mark.asyncio
async def test_forced_plugin_fails_when_required_dependency_is_disabled():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": False},
                "dependent": {"enabled": True},
            }
        }
    )
    fake = FakePlugin()
    dependent = DependentPlugin()
    manager = PluginManager(settings=settings, plugins=[fake, dependent])

    with pytest.raises(PluginDependencyError, match="inactive required dependency"):
        await manager.startup()

    assert fake.events == []
    assert dependent.events == []


@pytest.mark.asyncio
async def test_auto_plugin_is_disabled_when_required_dependency_is_disabled():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": False},
                "dependent": {"enabled": None},
            }
        }
    )
    fake = FakePlugin()
    dependent = DependentPlugin()
    manager = PluginManager(settings=settings, plugins=[fake, dependent])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert dependent.events == []
    assert snapshot["dependent"].status is HealthState.DISABLED
    assert snapshot["dependent"].details == {"inactive_dependencies": ["fake"]}


@pytest.mark.asyncio
async def test_auto_plugin_is_disabled_when_required_dependency_is_auto_skipped():
    settings = InfraSettings(
        infra={
            "plugins": {
                "missing": {"enabled": None},
                "depends_on_missing": {"enabled": None},
            }
        }
    )
    missing = MissingDependencyPlugin()
    dependent = DependsOnMissingPlugin()
    manager = PluginManager(settings=settings, plugins=[missing, dependent])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert missing.events == []
    assert dependent.events == []
    assert snapshot["depends_on_missing"].status is HealthState.DISABLED
    assert snapshot["depends_on_missing"].details == {"inactive_dependencies": ["missing"]}


@pytest.mark.asyncio
async def test_forced_plugin_fails_when_required_dependency_is_unknown():
    settings = InfraSettings(
        infra={"plugins": {"depends_on_absent": {"enabled": True}}}
    )
    plugin = DependsOnAbsentPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(PluginDependencyError, match="unknown required dependency"):
        await manager.startup()

    assert plugin.events == []


@pytest.mark.asyncio
async def test_auto_plugin_is_disabled_when_required_dependency_is_unknown():
    settings = InfraSettings(
        infra={"plugins": {"depends_on_absent": {"enabled": None}}}
    )
    plugin = DependsOnAbsentPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == []
    assert snapshot["depends_on_absent"].status is HealthState.DISABLED
    assert snapshot["depends_on_absent"].details == {"missing_dependencies": ["absent"]}


@pytest.mark.asyncio
async def test_disabled_plugin_with_unknown_required_dependency_is_skipped():
    settings = InfraSettings(
        infra={"plugins": {"depends_on_absent": {"enabled": False}}}
    )
    plugin = DependsOnAbsentPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == []
    assert snapshot["depends_on_absent"].status is HealthState.DISABLED
    assert snapshot["depends_on_absent"].message == "disabled by config"


def test_duplicate_plugin_names_raise_dependency_error():
    settings = InfraSettings()

    with pytest.raises(PluginDependencyError, match="duplicate plugin name"):
        PluginManager(settings=settings, plugins=[FakePlugin(), FakePlugin()])


@pytest.mark.asyncio
async def test_forced_missing_plugin_config_fails_before_register_or_startup():
    settings = InfraSettings(
        infra={"plugins": {"strict": {"enabled": True, "config": {}}}}
    )
    plugin = StrictConfigPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(ValidationError):
        await manager.startup()

    assert plugin.events == []
    assert plugin.config is None


@pytest.mark.asyncio
async def test_auto_missing_plugin_config_is_disabled_before_register_or_startup():
    settings = InfraSettings(
        infra={"plugins": {"strict": {"enabled": None, "config": {}}}}
    )
    plugin = StrictConfigPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == []
    assert plugin.config is None
    assert snapshot["strict"].status is HealthState.DISABLED
    assert "config_error" in snapshot["strict"].details


@pytest.mark.asyncio
async def test_disabled_missing_plugin_config_is_skipped_without_validation():
    settings = InfraSettings(
        infra={"plugins": {"strict": {"enabled": False, "config": {}}}}
    )
    plugin = StrictConfigPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == []
    assert plugin.config is None
    assert snapshot["strict"].status is HealthState.DISABLED
    assert snapshot["strict"].message == "disabled by config"
    assert snapshot["strict"].details == {}
