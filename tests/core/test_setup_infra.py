from fastapi import FastAPI

from infra import InfraSettings, setup_infra
from infra.core.context import InfraContext
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata


class SimplePlugin:
    metadata = PluginMetadata(name="simple", version="1.0.0", default_enabled=True)
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["simple"] = {"ready": True}

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("simple", HealthState.HEALTHY)


def test_setup_infra_attaches_context_to_app():
    app = FastAPI()
    settings = InfraSettings()

    infra = setup_infra(app, settings, plugins=[SimplePlugin()])

    assert isinstance(infra, InfraContext)
    assert app.state.infra is infra
    assert infra.get("simple") is None


def test_context_get_returns_registered_service_after_manual_startup():
    app = FastAPI()
    settings = InfraSettings()
    infra = setup_infra(app, settings, plugins=[SimplePlugin()])

    import anyio

    anyio.run(infra.startup)

    assert infra.get("simple") == {"ready": True}
