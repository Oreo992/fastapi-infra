from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


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


class HealthRegistry:
    def __init__(self) -> None:
        self._statuses: dict[str, HealthStatus] = {}

    def set_status(self, status: HealthStatus) -> None:
        self._statuses[status.name] = status.model_copy(deep=True)

    def snapshot(self) -> dict[str, HealthStatus]:
        return {name: status.model_copy(deep=True) for name, status in self._statuses.items()}
