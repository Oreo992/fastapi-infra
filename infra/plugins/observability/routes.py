from typing import Any

from fastapi import FastAPI, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, PlainTextResponse

from infra.core.health import HealthState


def install_observability_routes(app: FastAPI, infra: Any, prefix: str = "") -> None:
    route_prefix = prefix.rstrip("/")

    @app.get(f"{route_prefix}/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(content=jsonable_encoder(infra.health.snapshot()))

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
            content=jsonable_encoder({"statuses": statuses}),
        )

    @app.get(f"{route_prefix}/metrics")
    def metrics() -> Response:
        observability = infra.get("observability")
        if observability is None:
            return PlainTextResponse("")

        lines: list[str] = []
        for name, value in getattr(observability, "counters", {}).items():
            lines.append(f"{name} {value}")
        for name, values in getattr(observability, "timers", {}).items():
            lines.append(f"{name}_count {len(values)}")
            lines.append(f"{name}_sum {sum(values)}")

        return PlainTextResponse("\n".join(lines))
