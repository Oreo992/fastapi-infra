from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata


class NotificationResult(BaseModel):
    id: str
    channel: str
    recipient: str
    subject: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str


class NoopNotificationService:
    def __init__(self) -> None:
        self.results: list[NotificationResult] = []

    async def send(
        self,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        result = NotificationResult(
            id=f"ntf_{uuid4().hex}",
            channel=channel,
            recipient=recipient,
            subject=subject,
            body=body,
            metadata=metadata or {},
            status="skipped",
        )
        self.results.append(result)
        return result


class NotificationsPlugin:
    metadata = PluginMetadata(
        name="notifications",
        version="1.0.0",
        provides=["notifications"],
    )
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["notifications"] = NoopNotificationService()

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("notifications", HealthState.HEALTHY)
