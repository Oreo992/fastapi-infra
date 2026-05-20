from contextlib import asynccontextmanager

from fastapi import FastAPI

from infra.config.models import InfraSettings
from infra.core.context import InfraContext
from infra.plugins.contract import InfraPlugin
from infra.plugins.discovery import get_available_plugins


def setup_infra(
    app: FastAPI,
    settings: InfraSettings | None = None,
    plugins: list[InfraPlugin] | None = None,
    *,
    health_check_timeout_seconds: float | None = 5.0,
) -> InfraContext:
    if hasattr(app.state, "infra"):
        raise RuntimeError("infra is already configured")

    resolved_settings = settings or InfraSettings()
    resolved_plugins = plugins if plugins is not None else get_available_plugins(resolved_settings)
    context = InfraContext(
        app=app,
        settings=resolved_settings,
        plugins=resolved_plugins,
        health_check_timeout_seconds=health_check_timeout_seconds,
    )
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
