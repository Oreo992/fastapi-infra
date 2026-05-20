import asyncio

import pytest
from pydantic import BaseModel, ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.manager import PluginDependencyError, PluginManager


class FakeConfig(BaseModel):
    value: str = "ok"


class FakePlugin:
    metadata: PluginMetadata = PluginMetadata(name="fake", version="1.0.0", provides=["fake"])
    config_model: type[BaseModel] | None = FakeConfig

    def __init__(self) -> None:
        self.events: list[str] = []

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        service_name = self.metadata.provides[0] if self.metadata.provides else self.metadata.name
        ctx.services[service_name] = self

    async def startup(self, ctx: PluginContext) -> None:
        self.events.append("startup")

    async def shutdown(self, ctx: PluginContext) -> None:
        self.events.append("shutdown")

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("fake", HealthState.HEALTHY)


class ValidatingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="validating",
        version="1.0.0",
        default_enabled=True,
        provides=["validating"],
    )

    def validate_config(self, config: FakeConfig) -> None:
        self.events.append("validate_config")
        if config.value == "bad":
            raise ValueError("invalid validated config")


class DependentPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="dependent",
        version="1.0.0",
        dependencies=["fake"],
        default_enabled=True,
        provides=["dependent"],
    )


class MissingDependencyPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="missing",
        version="1.0.0",
        optional_dependencies=["package_that_does_not_exist_fastapi_infra"],
        default_enabled=True,
        provides=["missing"],
    )


class DependsOnMissingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="depends_on_missing",
        version="1.0.0",
        dependencies=["missing"],
        default_enabled=True,
        provides=["depends_on_missing"],
    )


class DependsOnAbsentPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="depends_on_absent",
        version="1.0.0",
        dependencies=["absent"],
        default_enabled=True,
        provides=["depends_on_absent"],
    )


class HealthFailingPlugin(FakePlugin):
    metadata = PluginMetadata(name="health_failing", version="1.0.0", provides=["health_failing"])

    async def health_check(self, ctx: PluginContext):
        raise RuntimeError("health failed")


class StartupFailingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="startup_failing",
        version="1.0.0",
        provides=["startup_failing"],
    )

    async def startup(self, ctx: PluginContext) -> None:
        self.events.append("startup")
        raise RuntimeError("startup failed")


class RegisterThenStartupFailingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="register_then_startup_failing",
        version="1.0.0",
        provides=["leaky"],
    )

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        ctx.services["leaky"] = self

    async def startup(self, ctx: PluginContext) -> None:
        self.events.append("startup")
        raise RuntimeError("startup failed")


class UnhealthyPlugin(FakePlugin):
    metadata = PluginMetadata(name="unhealthy", version="1.0.0", provides=["unhealthy"])

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("unhealthy", HealthState.UNHEALTHY)


class DegradedPlugin(FakePlugin):
    metadata = PluginMetadata(name="degraded", version="1.0.0", provides=["degraded"])

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("degraded", HealthState.DEGRADED)


class RefreshableHealthPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="refreshable_health",
        version="1.0.0",
        provides=["refreshable_health"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.healthy = True

    async def health_check(self, ctx: PluginContext):
        if self.healthy:
            return ctx.health_status("refreshable_health", HealthState.HEALTHY)
        return ctx.health_status("refreshable_health", HealthState.UNHEALTHY, "lost connection")


class RefreshHealthFailingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="refresh_health_failing",
        version="1.0.0",
        provides=["refresh_health_failing"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.raise_on_health = False

    async def health_check(self, ctx: PluginContext):
        if self.raise_on_health:
            raise RuntimeError("probe failed")
        return ctx.health_status("refresh_health_failing", HealthState.HEALTHY)


class TimeoutRefreshHealthPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="timeout_refresh_health",
        version="1.0.0",
        provides=["timeout_refresh_health"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.timeout_on_health = False

    async def health_check(self, ctx: PluginContext):
        if self.timeout_on_health:
            await asyncio.sleep(1)
        return ctx.health_status("timeout_refresh_health", HealthState.HEALTHY)


class StartupHealthTimeoutPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="startup_health_timeout",
        version="1.0.0",
        provides=["startup_health_timeout"],
    )

    async def health_check(self, ctx: PluginContext):
        await asyncio.sleep(1)
        return ctx.health_status("startup_health_timeout", HealthState.HEALTHY)


class ShutdownFailingRollbackPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="shutdown_failing_rollback",
        version="1.0.0",
        provides=["shutdown_failing_rollback"],
    )

    def __init__(self, attempts: list[str]) -> None:
        super().__init__()
        self.attempts = attempts

    async def shutdown(self, ctx: PluginContext) -> None:
        self.events.append("shutdown")
        self.attempts.append("shutdown_failing_rollback")
        raise RuntimeError("shutdown failed")


class RetryableShutdownFailingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="retryable_shutdown_failing",
        version="1.0.0",
        provides=["retryable_shutdown_failing"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.shutdown_attempts = 0

    async def shutdown(self, ctx: PluginContext) -> None:
        self.events.append("shutdown")
        self.shutdown_attempts += 1
        if self.shutdown_attempts == 1:
            raise RuntimeError("shutdown failed")


class TrackingRollbackPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="tracking_rollback",
        version="1.0.0",
        provides=["tracking_rollback"],
    )

    def __init__(self, attempts: list[str]) -> None:
        super().__init__()
        self.attempts = attempts

    async def shutdown(self, ctx: PluginContext) -> None:
        self.events.append("shutdown")
        self.attempts.append("tracking_rollback")


class LaterStartupFailingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="later_startup_failing",
        version="1.0.0",
        provides=["later_startup_failing"],
    )

    async def startup(self, ctx: PluginContext) -> None:
        self.events.append("startup")
        raise RuntimeError("original startup failed")


class StrictConfig(BaseModel):
    value: int


class StrictConfigPlugin(FakePlugin):
    metadata = PluginMetadata(name="strict", version="1.0.0", default_enabled=True)
    config_model = StrictConfig

    def __init__(self) -> None:
        super().__init__()
        self.config: StrictConfig | None = None

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        if not isinstance(ctx.config, StrictConfig):
            raise AssertionError("expected strict plugin config")
        self.config = ctx.config


class OverwritingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="overwriting",
        version="1.0.0",
        dependencies=["fake"],
        provides=["overwriting"],
    )

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        ctx.services["fake"] = "replacement"


class RemovingPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="removing",
        version="1.0.0",
        dependencies=["fake"],
        provides=["removing"],
    )

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        ctx.services.pop("fake", None)


class UndeclaredServicePlugin(FakePlugin):
    metadata = PluginMetadata(name="undeclared", version="1.0.0", provides=["declared"])

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        ctx.services["surprise"] = self


class PrimaryAndExtraServicePlugin(FakePlugin):
    metadata = PluginMetadata(name="primary_extra", version="1.0.0", provides=["primary"])

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        ctx.services["primary"] = self
        ctx.services["extra"] = object()


class ConfiguredServiceConfig(BaseModel):
    service: str = "configured"


class ConfiguredServicePlugin(FakePlugin):
    metadata = PluginMetadata(
        name="configured_service",
        version="1.0.0",
        provides=["configured_service"],
        service_name_config="service",
    )
    config_model = ConfiguredServiceConfig

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        config = ctx.config if isinstance(ctx.config, ConfiguredServiceConfig) else None
        ctx.services[config.service if config is not None else "configured"] = self


def test_plugin_metadata_defaults_to_disabled():
    metadata = PluginMetadata(name="plain", version="1.0.0")

    assert metadata.default_enabled is False


@pytest.mark.asyncio
async def test_enabled_plugin_registers_starts_and_stops():
    settings = InfraSettings(infra={"plugins": {"fake": {"enabled": True}}})
    plugin = FakePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()
    await manager.shutdown()

    assert plugin.events == ["register", "startup", "shutdown"]
    assert manager.get("fake", default=None) is None


@pytest.mark.asyncio
async def test_manager_can_start_again_after_clean_shutdown():
    settings = InfraSettings(infra={"plugins": {"fake": {"enabled": True}}})
    plugin = FakePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()
    first_service = manager.get("fake")
    await manager.shutdown()

    assert first_service is plugin
    assert manager.get("fake", default=None) is None

    await manager.startup()
    await manager.shutdown()

    assert plugin.events == [
        "register",
        "startup",
        "shutdown",
        "register",
        "startup",
        "shutdown",
    ]


@pytest.mark.asyncio
async def test_unknown_configured_plugin_fails_fast():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": True},
                "typo": {"enabled": False},
            }
        }
    )
    plugin = FakePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(PluginDependencyError, match="unknown configured plugin: typo"):
        await manager.startup()

    assert plugin.events == []


@pytest.mark.asyncio
async def test_repeated_startup_before_shutdown_raises_without_double_starting():
    settings = InfraSettings(infra={"plugins": {"fake": {"enabled": True}}})
    plugin = FakePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    with pytest.raises(RuntimeError, match="already started"):
        await manager.startup()

    assert plugin.events == ["register", "startup"]
    assert manager.started_plugins == ["fake"]
    assert manager.active_plugins == {"fake"}


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
    assert failing.events == ["register", "startup", "shutdown"]


@pytest.mark.asyncio
async def test_current_plugin_startup_failure_rolls_back_and_does_not_leak_services_or_context():
    settings = InfraSettings(
        infra={"plugins": {"register_then_startup_failing": {"enabled": True}}}
    )
    plugin = RegisterThenStartupFailingPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(RuntimeError, match="startup failed"):
        await manager.startup()

    assert plugin.events == ["register", "startup", "shutdown"]
    assert manager.get("leaky", default=None) is None
    assert "register_then_startup_failing" not in manager._contexts


@pytest.mark.asyncio
async def test_shutdown_failure_keeps_state_for_retry():
    settings = InfraSettings(infra={"plugins": {"retryable_shutdown_failing": {"enabled": True}}})
    plugin = RetryableShutdownFailingPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    with pytest.raises(RuntimeError, match="shutdown failed"):
        await manager.shutdown()

    assert plugin.events == ["register", "startup", "shutdown"]
    assert manager.started_plugins == ["retryable_shutdown_failing"]
    assert manager.active_plugins == {"retryable_shutdown_failing"}
    assert "retryable_shutdown_failing" in manager._contexts

    await manager.shutdown()

    assert plugin.events == ["register", "startup", "shutdown", "shutdown"]
    assert manager.started_plugins == []
    assert manager.active_plugins == set()
    assert "retryable_shutdown_failing" not in manager._contexts
    assert manager.get("retryable_shutdown_failing", default=None) is None


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
    assert failing.events == ["register", "startup", "shutdown"]


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
    assert startup_failing.events == ["register", "startup", "shutdown"]
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
async def test_startup_health_check_timeout_fails_startup_and_rolls_back():
    settings = InfraSettings(infra={"plugins": {"startup_health_timeout": {"enabled": True}}})
    plugin = StartupHealthTimeoutPlugin()
    manager = PluginManager(
        settings=settings,
        plugins=[plugin],
        health_check_timeout_seconds=0.01,
    )

    with pytest.raises(PluginDependencyError, match="plugin is unhealthy"):
        await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == ["register", "startup", "shutdown"]
    assert snapshot["startup_health_timeout"].status is HealthState.UNHEALTHY
    assert snapshot["startup_health_timeout"].message == "health check timed out after 0.01s"
    assert manager.started_plugins == []
    assert manager.active_plugins == set()


def test_plugin_manager_rejects_negative_health_check_timeout() -> None:
    with pytest.raises(ValueError, match="health_check_timeout_seconds"):
        PluginManager(
            settings=InfraSettings(),
            plugins=[],
            health_check_timeout_seconds=-1,
        )


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
async def test_refresh_health_updates_active_plugin_status():
    settings = InfraSettings(infra={"plugins": {"refreshable_health": {"enabled": True}}})
    plugin = RefreshableHealthPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()
    plugin.healthy = False

    snapshot = await manager.refresh_health()

    assert snapshot["refreshable_health"].status is HealthState.UNHEALTHY
    assert snapshot["refreshable_health"].message == "lost connection"
    assert manager.health.snapshot()["refreshable_health"].status is HealthState.UNHEALTHY
    assert manager.active_plugins == {"refreshable_health"}


@pytest.mark.asyncio
async def test_refresh_health_marks_probe_exceptions_unhealthy_without_raising():
    settings = InfraSettings(infra={"plugins": {"refresh_health_failing": {"enabled": True}}})
    plugin = RefreshHealthFailingPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()
    plugin.raise_on_health = True

    snapshot = await manager.refresh_health()

    assert snapshot["refresh_health_failing"].status is HealthState.UNHEALTHY
    assert snapshot["refresh_health_failing"].message == "probe failed"
    assert manager.active_plugins == {"refresh_health_failing"}


@pytest.mark.asyncio
async def test_refresh_health_marks_probe_timeouts_unhealthy_without_raising():
    settings = InfraSettings(infra={"plugins": {"timeout_refresh_health": {"enabled": True}}})
    plugin = TimeoutRefreshHealthPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()
    plugin.timeout_on_health = True

    snapshot = await manager.refresh_health(timeout_seconds=0.01)

    assert snapshot["timeout_refresh_health"].status is HealthState.UNHEALTHY
    assert snapshot["timeout_refresh_health"].message == "health check timed out after 0.01s"
    assert manager.active_plugins == {"timeout_refresh_health"}


@pytest.mark.asyncio
async def test_refresh_health_checks_active_plugins_concurrently():
    entered: list[str] = []
    release = asyncio.Event()

    class CoordinatedHealthPlugin(FakePlugin):
        config_model = FakeConfig

        def __init__(self, name: str) -> None:
            super().__init__()
            self.metadata = PluginMetadata(name=name, version="1.0.0", provides=[name])
            self.coordinate = False

        async def health_check(self, ctx: PluginContext):
            if not self.coordinate:
                return ctx.health_status(self.metadata.name, HealthState.HEALTHY)
            entered.append(self.metadata.name)
            if len(entered) == 2:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=0.2)
            return ctx.health_status(self.metadata.name, HealthState.HEALTHY)

    first = CoordinatedHealthPlugin("first_coordinated")
    second = CoordinatedHealthPlugin("second_coordinated")
    settings = InfraSettings(
        infra={
            "plugins": {
                "first_coordinated": {"enabled": True},
                "second_coordinated": {"enabled": True},
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[first, second])

    await manager.startup()
    first.coordinate = True
    second.coordinate = True

    snapshot = await manager.refresh_health(timeout_seconds=0.5)

    assert entered == ["first_coordinated", "second_coordinated"]
    assert snapshot["first_coordinated"].status is HealthState.HEALTHY
    assert snapshot["second_coordinated"].status is HealthState.HEALTHY


@pytest.mark.asyncio
async def test_refresh_health_rejects_negative_timeout():
    manager = PluginManager(settings=InfraSettings(), plugins=[])

    with pytest.raises(ValueError, match="timeout_seconds"):
        await manager.refresh_health(timeout_seconds=-1)


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
    settings = InfraSettings(infra={"plugins": {"depends_on_absent": {"enabled": True}}})
    plugin = DependsOnAbsentPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(PluginDependencyError, match="unknown required dependency"):
        await manager.startup()

    assert plugin.events == []


@pytest.mark.asyncio
async def test_auto_plugin_is_disabled_when_required_dependency_is_unknown():
    settings = InfraSettings(infra={"plugins": {"depends_on_absent": {"enabled": None}}})
    plugin = DependsOnAbsentPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == []
    assert snapshot["depends_on_absent"].status is HealthState.DISABLED
    assert snapshot["depends_on_absent"].details == {"missing_dependencies": ["absent"]}


@pytest.mark.asyncio
async def test_disabled_plugin_with_unknown_required_dependency_is_skipped():
    settings = InfraSettings(infra={"plugins": {"depends_on_absent": {"enabled": False}}})
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


def test_plugin_manager_exports_plugin_manifest():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": True, "config": {"value": "configured"}},
                "dependent": {"enabled": False},
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[FakePlugin(), DependentPlugin()])

    manifest = manager.manifest()

    assert manifest == {
        "fake": {
            "name": "fake",
            "version": "1.0.0",
            "default_enabled": False,
            "dependencies": [],
            "optional_dependencies": [],
            "provides": ["fake"],
            "service_name_config": None,
            "configured_services": ["fake"],
            "configured_enabled": True,
            "config_model": "FakeConfig",
            "config_schema": FakeConfig.model_json_schema(),
            "recommended_extras": [],
            "env_vars": [],
            "local_config_example": {},
            "production_config_example": {},
            "production_dependencies": [],
            "service_keys": {},
            "service_references": {},
            "migrations": [],
            "scaffold_files": [],
            "scaffold_readme_sections": [],
            "release_check_notes": [],
        },
        "dependent": {
            "name": "dependent",
            "version": "1.0.0",
            "default_enabled": True,
            "dependencies": ["fake"],
            "optional_dependencies": [],
            "provides": ["dependent"],
            "service_name_config": None,
            "configured_services": ["dependent"],
            "configured_enabled": False,
            "config_model": "FakeConfig",
            "config_schema": FakeConfig.model_json_schema(),
            "recommended_extras": [],
            "env_vars": [],
            "local_config_example": {},
            "production_config_example": {},
            "production_dependencies": [],
            "service_keys": {},
            "service_references": {},
            "migrations": [],
            "scaffold_files": [],
            "scaffold_readme_sections": [],
            "release_check_notes": [],
        },
    }


def test_plugin_manifest_reports_configured_service_names():
    settings = InfraSettings(
        infra={
            "plugins": {
                "configured_service": {
                    "enabled": True,
                    "config": {"service": "jobs"},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[ConfiguredServicePlugin()])

    manifest = manager.manifest()

    assert manifest["configured_service"]["provides"] == ["configured_service"]
    assert manifest["configured_service"]["service_name_config"] == "service"
    assert manifest["configured_service"]["configured_services"] == [
        "configured_service",
        "jobs",
    ]


@pytest.mark.asyncio
async def test_forced_missing_plugin_config_fails_before_register_or_startup():
    settings = InfraSettings(infra={"plugins": {"strict": {"enabled": True, "config": {}}}})
    plugin = StrictConfigPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(ValidationError):
        await manager.startup()

    assert plugin.events == []
    assert plugin.config is None


@pytest.mark.asyncio
async def test_plugin_validate_config_fails_before_register_or_startup():
    settings = InfraSettings(
        infra={
            "plugins": {
                "validating": {
                    "enabled": True,
                    "config": {"value": "bad"},
                }
            }
        }
    )
    plugin = ValidatingPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(ValueError, match="invalid validated config"):
        await manager.startup()

    assert plugin.events == ["validate_config"]


@pytest.mark.asyncio
async def test_auto_plugin_validate_config_error_disables_before_register_or_startup():
    settings = InfraSettings(
        infra={
            "plugins": {
                "validating": {
                    "enabled": None,
                    "config": {"value": "bad"},
                }
            }
        }
    )
    plugin = ValidatingPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == ["validate_config"]
    assert snapshot["validating"].status is HealthState.DISABLED
    assert "config_error" in snapshot["validating"].details


@pytest.mark.asyncio
async def test_auto_missing_plugin_config_is_disabled_before_register_or_startup():
    settings = InfraSettings(infra={"plugins": {"strict": {"enabled": None, "config": {}}}})
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
    settings = InfraSettings(infra={"plugins": {"strict": {"enabled": False, "config": {}}}})
    plugin = StrictConfigPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    snapshot = manager.health.snapshot()
    assert plugin.events == []
    assert plugin.config is None
    assert snapshot["strict"].status is HealthState.DISABLED
    assert snapshot["strict"].message == "disabled by config"
    assert snapshot["strict"].details == {}


@pytest.mark.asyncio
async def test_plugin_cannot_overwrite_existing_service():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": True},
                "overwriting": {"enabled": True},
            }
        }
    )
    fake = FakePlugin()
    overwriting = OverwritingPlugin()
    manager = PluginManager(settings=settings, plugins=[fake, overwriting])

    with pytest.raises(PluginDependencyError, match="overwrote existing services: fake"):
        await manager.startup()

    assert manager.get("fake", default=None) is None
    assert overwriting.events == ["register"]


@pytest.mark.asyncio
async def test_plugin_cannot_remove_existing_service():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": True},
                "removing": {"enabled": True},
            }
        }
    )
    fake = FakePlugin()
    removing = RemovingPlugin()
    manager = PluginManager(settings=settings, plugins=[fake, removing])

    with pytest.raises(PluginDependencyError, match="removed services it does not own: fake"):
        await manager.startup()

    assert manager.get("fake", default=None) is None
    assert removing.events == ["register"]


@pytest.mark.asyncio
async def test_plugin_cannot_register_undeclared_service_name():
    settings = InfraSettings(infra={"plugins": {"undeclared": {"enabled": True}}})
    plugin = UndeclaredServicePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(PluginDependencyError, match="registered undeclared services: surprise"):
        await manager.startup()

    assert manager.get("surprise", default=None) is None


@pytest.mark.asyncio
async def test_plugin_cannot_register_extra_implementation_services():
    settings = InfraSettings(infra={"plugins": {"primary_extra": {"enabled": True}}})
    plugin = PrimaryAndExtraServicePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(PluginDependencyError, match="registered undeclared services: extra"):
        await manager.startup()

    assert manager.get("primary", default=None) is None
    assert manager.get("extra", default=None) is None


@pytest.mark.asyncio
async def test_plugin_can_register_service_name_declared_by_config_field():
    settings = InfraSettings(
        infra={
            "plugins": {
                "configured_service": {
                    "enabled": True,
                    "config": {"service": "jobs"},
                }
            }
        }
    )
    plugin = ConfiguredServicePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    assert manager.get("jobs") is plugin
