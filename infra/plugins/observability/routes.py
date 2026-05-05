import re
from enum import Enum
from typing import Any

from fastapi import FastAPI, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from infra.core.health import HealthState

_METRIC_NAME_RE = re.compile(r"[^a-zA-Z0-9_:]")
_METRIC_START_RE = re.compile(r"^[a-zA-Z_:]")


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


def _ensure_routes_available(app: FastAPI, paths: set[str]) -> None:
    existing_paths = {
        route.path
        for route in app.routes
        if "GET" in getattr(route, "methods", set())
    }
    collisions = sorted(paths & existing_paths)
    if collisions:
        raise RuntimeError(
            "observability route collision for: " + ", ".join(collisions)
        )


def install_observability_routes(app: FastAPI, infra: Any, prefix: str = "") -> None:
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
    def readyz() -> JSONResponse:
        statuses = infra.health.snapshot()
        is_unhealthy = any(
            getattr(health_status.status, "value", health_status.status)
            == HealthState.UNHEALTHY.value
            for health_status in statuses.values()
        )
        status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if is_unhealthy
            else status.HTTP_200_OK
        )
        return JSONResponse(
            status_code=status_code,
            content=_json_safe({"statuses": statuses}),
        )

    @app.get(f"{route_prefix}/metrics")
    def metrics() -> Response:
        observability = infra.get("observability")
        if observability is None:
            return PlainTextResponse("")

        lines: list[str] = []
        for name, value in getattr(observability, "counters", {}).items():
            lines.append(f"{_sanitize_metric_name(name)} {value}")
        for name, values in getattr(observability, "timers", {}).items():
            metric_name = _sanitize_metric_name(name)
            lines.append(f"{metric_name}_count {len(values)}")
            lines.append(f"{metric_name}_sum {sum(values)}")

        return PlainTextResponse("\n".join(lines))
