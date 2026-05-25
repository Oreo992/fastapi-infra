import re
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any, ContextManager, Literal, cast

from pydantic import BaseModel, Field

from infra.core.health import HealthRegistry, HealthStatus

_METRIC_NAME_RE = re.compile(r"[^a-zA-Z0-9_:]")
_METRIC_START_RE = re.compile(r"^[a-zA-Z_:]")


def _sanitize_metric_name(name: str) -> str:
    sanitized = _METRIC_NAME_RE.sub("_", str(name))
    if not sanitized:
        return "_"
    if not _METRIC_START_RE.match(sanitized):
        return f"_{sanitized}"
    return sanitized


class ObservabilityEvent(BaseModel):
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PrometheusMetricsBackend:
    content_type = "text/plain; version=0.0.4; charset=utf-8"

    def __init__(self) -> None:
        try:
            from prometheus_client import CollectorRegistry, Counter, Summary
        except ImportError as exc:
            raise RuntimeError(
                "prometheus metrics backend requires prometheus-client; "
                "install fastapi-infra[observability]"
            ) from exc

        self._counter_cls = Counter
        self._summary_cls = Summary
        self._registry = CollectorRegistry()
        self._counters: dict[str, Any] = {}
        self._summaries: dict[str, Any] = {}

    def increment(self, name: str, amount: int = 1) -> None:
        metric_name = _sanitize_metric_name(name)
        counter = self._counters.get(metric_name)
        if counter is None:
            counter = self._counter_cls(metric_name, metric_name, registry=self._registry)
            self._counters[metric_name] = counter
        counter.inc(amount)

    def timing(self, name: str, value: float) -> None:
        metric_name = _sanitize_metric_name(name)
        summary = self._summaries.get(metric_name)
        if summary is None:
            summary = self._summary_cls(metric_name, metric_name, registry=self._registry)
            self._summaries[metric_name] = summary
        summary.observe(value)

    def render(self) -> str:
        from prometheus_client import generate_latest

        return cast(str, generate_latest(self._registry).decode("utf-8"))


class OpenTelemetryTracingBackend:
    def __init__(self) -> None:
        try:
            from opentelemetry import trace
        except ImportError as exc:
            raise RuntimeError(
                "opentelemetry tracing backend requires opentelemetry-api; "
                "install fastapi-infra[observability]"
            ) from exc

        self._tracer = trace.get_tracer("fastapi_infra.observability")

    @contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> Iterator[Any]:
        with self._tracer.start_as_current_span(name) as span:
            for key, value in (attributes or {}).items():
                span.set_attribute(key, value)
            yield span


class ObservabilityService:
    def __init__(
        self,
        health: HealthRegistry,
        metrics_backend: Literal["memory", "prometheus"] = "memory",
        tracing_backend: Literal["none", "opentelemetry"] = "none",
    ) -> None:
        self._health = health
        self._metrics_backend = (
            PrometheusMetricsBackend() if metrics_backend == "prometheus" else None
        )
        self._tracing_backend = (
            OpenTelemetryTracingBackend() if tracing_backend == "opentelemetry" else None
        )
        self.counters: dict[str, int] = {}
        self.timers: dict[str, list[float]] = {}
        self.events: list[ObservabilityEvent] = []

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount
        if self._metrics_backend is not None:
            self._metrics_backend.increment(name, amount)

    def timing(self, name: str, value: float) -> None:
        self.timers.setdefault(name, []).append(value)
        if self._metrics_backend is not None:
            self._metrics_backend.timing(name, value)

    def event(self, name: str, payload: dict[str, Any] | None = None) -> None:
        self.events.append(ObservabilityEvent(name=name, payload=payload or {}))

    def render_metrics(self) -> str | None:
        if self._metrics_backend is None:
            return None
        return self._metrics_backend.render()

    def span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> ContextManager[Any]:
        if self._tracing_backend is None:
            return nullcontext()
        return self._tracing_backend.span(name, attributes)

    def health_snapshot(self) -> dict[str, HealthStatus]:
        return self._health.snapshot()
