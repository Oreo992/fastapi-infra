from typing import Any

from pydantic import BaseModel, Field

from infra.core.health import HealthRegistry, HealthStatus


class ObservabilityEvent(BaseModel):
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ObservabilityService:
    def __init__(self, health: HealthRegistry) -> None:
        self._health = health
        self.counters: dict[str, int] = {}
        self.timers: dict[str, list[float]] = {}
        self.events: list[ObservabilityEvent] = []

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def timing(self, name: str, value: float) -> None:
        self.timers.setdefault(name, []).append(value)

    def event(self, name: str, payload: dict[str, Any] | None = None) -> None:
        self.events.append(ObservabilityEvent(name=name, payload=payload or {}))

    def health_snapshot(self) -> dict[str, HealthStatus]:
        return self._health.snapshot()
