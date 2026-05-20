import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

REDACTED = "[redacted]"
SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(['\"]?\b(api[_-]?key|authorization|credential|password|secret|token)"
    r"\b['\"]?\s*[:=]\s*)(['\"]?)([^'\"\s,;}]+)(['\"]?)"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+([^\s,;]+)")


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


class HealthStatus(BaseModel):
    name: str
    status: HealthState
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def redact_secrets(self) -> "HealthStatus":
        if self.message is not None:
            self.message = redact_secret_text(self.message)
        self.details = redact_secret_value(self.details)
        return self


def redact_secret_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_secret_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {
            item_key: redact_secret_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_secret_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secret_value(item) for item in value)
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


def redact_secret_text(value: str) -> str:
    redacted = BEARER_TOKEN_RE.sub(f"Bearer {REDACTED}", value)
    return SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(3)}{REDACTED}{match.group(5)}",
        redacted,
    )


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SECRET_KEY_PARTS)


class HealthRegistry:
    def __init__(self) -> None:
        self._statuses: dict[str, HealthStatus] = {}

    def set_status(self, status: HealthStatus) -> None:
        self._statuses[status.name] = status.model_copy(deep=True)

    def snapshot(self) -> dict[str, HealthStatus]:
        return {name: status.model_copy(deep=True) for name, status in self._statuses.items()}
