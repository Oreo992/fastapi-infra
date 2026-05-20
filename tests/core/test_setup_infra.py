import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra import InfraSettings, setup_infra
from infra.core.context import InfraContext
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata


class SimplePlugin:
    metadata = PluginMetadata(
        name="simple",
        version="1.0.0",
        default_enabled=True,
        provides=["simple"],
    )
    config_model = None

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    def register(self, ctx: PluginContext) -> None:
        if self.events is not None:
            self.events.append("register")
        ctx.services["simple"] = {"ready": True}

    async def startup(self, ctx: PluginContext) -> None:
        if self.events is not None:
            self.events.append("startup")
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        if self.events is not None:
            self.events.append("shutdown")
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("simple", HealthState.HEALTHY)


class SlowHealthPlugin(SimplePlugin):
    metadata = PluginMetadata(
        name="slow_health",
        version="1.0.0",
        default_enabled=True,
        provides=["slow_health"],
    )

    def register(self, ctx: PluginContext) -> None:
        ctx.services["slow_health"] = {"ready": True}

    async def health_check(self, ctx: PluginContext):
        await asyncio.sleep(1)
        return ctx.health_status("slow_health", HealthState.HEALTHY)


def test_setup_infra_attaches_context_to_app():
    app = FastAPI()
    settings = InfraSettings()

    infra = setup_infra(app, settings, plugins=[SimplePlugin()])

    assert isinstance(infra, InfraContext)
    assert app.state.infra is infra
    assert infra.get("simple") is None


def test_setup_infra_passes_startup_health_check_timeout():
    app = FastAPI()
    infra = setup_infra(
        app,
        InfraSettings(),
        plugins=[SlowHealthPlugin()],
        health_check_timeout_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="timed out"):
        with TestClient(app):
            pass

    assert infra.health.snapshot()["slow_health"].status is HealthState.UNHEALTHY


def test_setup_infra_rejects_negative_health_check_timeout():
    app = FastAPI()

    with pytest.raises(ValueError, match="health_check_timeout_seconds"):
        setup_infra(
            app,
            InfraSettings(),
            plugins=[SimplePlugin()],
            health_check_timeout_seconds=-1,
        )


def test_context_get_returns_registered_service_after_manual_startup():
    app = FastAPI()
    settings = InfraSettings()
    infra = setup_infra(app, settings, plugins=[SimplePlugin()])

    import anyio

    anyio.run(infra.startup)

    assert infra.get("simple") == {"ready": True}


def test_setup_infra_rejects_repeated_configuration_without_extra_handlers():
    app = FastAPI()
    settings = InfraSettings()
    startup_count = len(app.router.on_startup)
    shutdown_count = len(app.router.on_shutdown)

    setup_infra(app, settings, plugins=[SimplePlugin()])

    with pytest.raises(RuntimeError, match="infra is already configured"):
        setup_infra(app, settings, plugins=[SimplePlugin()])

    assert len(app.router.on_startup) == startup_count
    assert len(app.router.on_shutdown) == shutdown_count


def test_setup_infra_does_not_append_router_lifecycle_handlers():
    app = FastAPI()
    startup_count = len(app.router.on_startup)
    shutdown_count = len(app.router.on_shutdown)

    setup_infra(app, InfraSettings(), plugins=[SimplePlugin()])

    assert len(app.router.on_startup) == startup_count
    assert len(app.router.on_shutdown) == shutdown_count


def test_registered_lifecycle_callbacks_startup_and_shutdown_plugin_once():
    app = FastAPI()
    settings = InfraSettings()
    events: list[str] = []
    setup_infra(app, settings, plugins=[SimplePlugin(events)])

    assert events == []

    with TestClient(app):
        assert events == ["register", "startup"]

    assert events == ["register", "startup", "shutdown"]


def test_setup_infra_composes_with_existing_lifespan():
    events: list[str] = []

    @asynccontextmanager
    async def existing_lifespan(app: FastAPI):
        events.append("existing_startup")
        yield
        events.append("existing_shutdown")

    app = FastAPI(lifespan=existing_lifespan)
    setup_infra(app, InfraSettings(), plugins=[SimplePlugin(events)])

    with TestClient(app):
        assert events == ["register", "startup", "existing_startup"]

    assert events == [
        "register",
        "startup",
        "existing_startup",
        "existing_shutdown",
        "shutdown",
    ]
