from collections.abc import Awaitable, Callable
from typing import Any

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata

WebhookHandler = Callable[[str, Any], Awaitable[Any]]


class WebhookDispatcher:
    def __init__(self) -> None:
        self._handlers: list[WebhookHandler] = []

    def register(self, handler: WebhookHandler) -> None:
        self._handlers.append(handler)

    async def dispatch(self, event: str, payload: Any) -> list[Any]:
        return [await handler(event, payload) for handler in self._handlers]


class WebhooksPlugin:
    metadata = PluginMetadata(
        name="webhooks",
        version="1.0.0",
        provides=["webhooks"],
    )
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["webhooks"] = WebhookDispatcher()

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("webhooks", HealthState.HEALTHY)
