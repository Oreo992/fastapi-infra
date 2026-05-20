import re
from enum import Enum
from inspect import isawaitable
from typing import Any

from fastapi import FastAPI, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from starlette.routing import Route

from infra.core.health import HealthState
from infra.core.services import ServiceKey

_METRIC_NAME_RE = re.compile(r"[^a-zA-Z0-9_:]")
_METRIC_START_RE = re.compile(r"^[a-zA-Z_:]")
_CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
_OBSERVABILITY_SERVICE = ServiceKey[Any]("observability")


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", fallback=str)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sanitize_metric_name(name: str) -> str:
    sanitized = _METRIC_NAME_RE.sub("_", str(name))
    if not sanitized:
        return "_"
    if not _METRIC_START_RE.match(sanitized):
        return f"_{sanitized}"
    return sanitized


def _format_metric_value(value: int | float) -> str:
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def render_prometheus_metrics(observability: Any) -> str:
    lines: list[str] = []
    for name, value in sorted(getattr(observability, "counters", {}).items()):
        metric_name = _sanitize_metric_name(name)
        lines.append(f"# TYPE {metric_name} counter")
        lines.append(f"{metric_name} {_format_metric_value(value)}")
    for name, values in sorted(getattr(observability, "timers", {}).items()):
        metric_name = _sanitize_metric_name(name)
        samples = [float(value) for value in values]
        lines.append(f"# TYPE {metric_name} summary")
        lines.append(f"{metric_name}_count {len(samples)}")
        lines.append(f"{metric_name}_sum {_format_metric_value(sum(samples))}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _ensure_routes_available(app: FastAPI, paths: set[str]) -> None:
    existing_paths = {
        route.path
        for route in app.routes
        if isinstance(route, Route) and "GET" in (route.methods or set())
    }
    collisions = sorted(paths & existing_paths)
    if collisions:
        raise RuntimeError("observability route collision for: " + ", ".join(collisions))


async def _readiness_snapshot(infra: Any, timeout_seconds: float | None) -> Any:
    refresh_health = getattr(infra, "refresh_health", None)
    if not callable(refresh_health):
        return infra.health.snapshot()
    refreshed = refresh_health(timeout_seconds=timeout_seconds)
    if isawaitable(refreshed):
        return await refreshed
    return refreshed


def install_observability_routes(
    app: FastAPI,
    infra: Any,
    prefix: str = "",
    *,
    readiness_timeout_seconds: float | None = 5.0,
) -> None:
    if readiness_timeout_seconds is not None and readiness_timeout_seconds < 0:
        raise ValueError("readiness_timeout_seconds must be greater than or equal to 0")
    route_prefix = prefix.rstrip("/")
    route_paths = {
        f"{route_prefix}/healthz",
        f"{route_prefix}/readyz",
        f"{route_prefix}/metrics",
    }
    _ensure_routes_available(app, route_paths)

    @app.get(f"{route_prefix}/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(content=_json_safe(infra.health.snapshot()))

    @app.get(f"{route_prefix}/readyz")
    async def readyz() -> JSONResponse:
        statuses = await _readiness_snapshot(infra, readiness_timeout_seconds)
        is_unhealthy = any(
            getattr(health_status.status, "value", health_status.status)
            == HealthState.UNHEALTHY.value
            for health_status in statuses.values()
        )
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE if is_unhealthy else status.HTTP_200_OK
        return JSONResponse(
            status_code=status_code,
            content=_json_safe({"statuses": statuses}),
        )

    @app.get(f"{route_prefix}/metrics")
    def metrics() -> Response:
        observability = infra.get(_OBSERVABILITY_SERVICE)
        if observability is None:
            return PlainTextResponse("", media_type=_CONTENT_TYPE_LATEST)
        if hasattr(observability, "render_metrics"):
            rendered = observability.render_metrics()
            if rendered is not None:
                return PlainTextResponse(rendered, media_type=_CONTENT_TYPE_LATEST)

        return PlainTextResponse(
            render_prometheus_metrics(observability),
            media_type=_CONTENT_TYPE_LATEST,
        )
