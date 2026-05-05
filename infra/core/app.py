from fastapi import FastAPI

from infra.config.models import InfraSettings
from infra.core.context import InfraContext
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.contract import InfraPlugin


def setup_infra(
    app: FastAPI,
    settings: InfraSettings | None = None,
    plugins: list[InfraPlugin] | None = None,
) -> InfraContext:
    if hasattr(app.state, "infra"):
        raise RuntimeError("infra is already configured")

    resolved_settings = settings or InfraSettings()
    resolved_plugins = plugins if plugins is not None else get_builtin_plugins()
    context = InfraContext(app=app, settings=resolved_settings, plugins=resolved_plugins)
    app.state.infra = context

    @app.on_event("startup")
    async def _infra_startup() -> None:
        await context.startup()

    @app.on_event("shutdown")
    async def _infra_shutdown() -> None:
        await context.shutdown()

    return context
