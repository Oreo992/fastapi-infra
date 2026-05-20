from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from time import perf_counter
from typing import Protocol, runtime_checkable

from fastapi import FastAPI, Request, Response


@runtime_checkable
class ObservabilityRecorder(Protocol):
    def increment(self, name: str, amount: int = 1) -> None: ...

    def timing(self, name: str, value: float) -> None: ...


def _get_observability_service(
    app: FastAPI,
    service: ObservabilityRecorder | None,
    service_name: str,
) -> ObservabilityRecorder | None:
    if service is not None:
        return service

    infra = getattr(app.state, "infra", None)
    if infra is not None and hasattr(infra, "get"):
        candidate = infra.get(service_name)
        return candidate if isinstance(candidate, ObservabilityRecorder) else None

    state_service = getattr(app.state, service_name, None)
    if isinstance(state_service, ObservabilityRecorder):
        return state_service

    return None


def install_observability_middleware(
    app: FastAPI,
    service: ObservabilityRecorder | None = None,
    service_name: str = "observability",
) -> None:
    @app.middleware("http")
    async def observability_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        recorder = _get_observability_service(app, service, service_name)
        span = getattr(recorder, "span", None) if recorder is not None else None
        span_context = (
            span(
                "http.request",
                {
                    "http.method": request.method,
                    "http.target": request.url.path,
                },
            )
            if callable(span)
            else nullcontext()
        )
        started_at = perf_counter()

        with span_context:
            try:
                response = await call_next(request)
            except Exception:
                if recorder is not None:
                    recorder.increment("http_requests_total")
                    recorder.increment("http_responses_status_500_total")
                    recorder.increment("http_request_errors_total")
                    recorder.timing(
                        "http_request_duration_seconds",
                        perf_counter() - started_at,
                    )
                raise

            if recorder is not None:
                recorder.increment("http_requests_total")
                recorder.increment(f"http_responses_status_{response.status_code}_total")
                recorder.timing(
                    "http_request_duration_seconds",
                    perf_counter() - started_at,
                )

            return response
