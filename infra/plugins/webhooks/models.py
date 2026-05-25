from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class WebhookEvent(BaseModel):
    id: str
    provider: str
    type: str
    payload: dict[str, Any]
    headers: dict[str, str] = Field(default_factory=dict)
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


def normalize_webhook_provider_name(provider: str) -> str:
    normalized = provider.strip().lower()
    if not normalized:
        raise ValueError("webhook provider must not be empty")
    return normalized
