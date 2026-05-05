from contextlib import asynccontextmanager

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
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _infra_lifespan(app: FastAPI):
        await context.startup()
        try:
            async with previous_lifespan(app):
                yield
        finally:
            await context.shutdown()

    app.router.lifespan_context = _infra_lifespan

    return context
