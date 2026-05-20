import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.core.health import HealthRegistry
from infra.plugins.observability import (
    ObservabilityService,
    install_observability_middleware,
    install_observability_routes,
)
from infra.plugins.services import OBSERVABILITY_SERVICE


class FakeInfra:
    def __init__(self, observability: ObservabilityService) -> None:
        self.health = HealthRegistry()
        self._services = {"observability": observability}

    def get(self, name: object, default: object | None = None) -> object | None:
        service_name = name.name if hasattr(name, "name") else name
        assert service_name == OBSERVABILITY_SERVICE.name
        return self._services.get(service_name, default)


def test_observability_middleware_records_real_successful_requests():
    service = ObservabilityService(HealthRegistry())
    app = FastAPI()

    @app.get("/items/{item_id}")
    def read_item(item_id: str):
        return {"item_id": item_id}

    install_observability_middleware(app, service=service)

    response = TestClient(app).get("/items/abc")

    assert response.status_code == 200
    assert service.counters["http_requests_total"] == 1
    assert service.counters["http_responses_status_200_total"] == 1
    assert service.timers["http_request_duration_seconds"]
    assert service.timers["http_request_duration_seconds"][0] >= 0


def test_observability_middleware_records_500_and_reraises_exceptions():
    service = ObservabilityService(HealthRegistry())
    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("boom")

    install_observability_middleware(app, service=service)

    client = TestClient(app)
    with pytest.raises(RuntimeError, match="boom"):
        client.get("/boom")

    assert service.counters["http_requests_total"] == 1
    assert service.counters["http_responses_status_500_total"] == 1
    assert service.counters["http_request_errors_total"] == 1
    assert service.timers["http_request_duration_seconds"]


def test_observability_middleware_metrics_route_exposes_real_request_metrics():
    service = ObservabilityService(HealthRegistry())
    infra = FakeInfra(service)
    app = FastAPI()
    app.state.infra = infra

    @app.post("/widgets")
    def create_widget():
        return {"ok": True}

    install_observability_middleware(app)
    install_observability_routes(app, infra)

    client = TestClient(app)
    response = client.post("/widgets")
    metrics = client.get("/metrics")

    assert response.status_code == 200
    assert metrics.status_code == 200
    assert "# TYPE http_requests_total counter" in metrics.text
    assert "http_requests_total 1" in metrics.text
    assert "# TYPE http_responses_status_200_total counter" in metrics.text
    assert "http_responses_status_200_total 1" in metrics.text
    assert "# TYPE http_request_duration_seconds summary" in metrics.text
    assert "http_request_duration_seconds_count 1" in metrics.text


def test_observability_middleware_wraps_request_in_optional_span():
    class SpanRecorder(ObservabilityService):
        def __init__(self) -> None:
            super().__init__(HealthRegistry())
            self.spans: list[tuple[str, dict[str, str | int | float | bool], str]] = []

        def span(
            self,
            name: str,
            attributes: dict[str, str | int | float | bool] | None = None,
        ):
            recorder = self

            class Span:
                def __enter__(self):
                    recorder.spans.append((name, attributes or {}, "enter"))

                def __exit__(self, exc_type, exc, traceback):
                    recorder.spans.append((name, attributes or {}, "exit"))

            return Span()

    service = SpanRecorder()
    app = FastAPI()

    @app.get("/traced")
    def traced():
        return {"ok": True}

    install_observability_middleware(app, service=service)

    response = TestClient(app).get("/traced")

    assert response.status_code == 200
    assert service.spans == [
        (
            "http.request",
            {"http.method": "GET", "http.target": "/traced"},
            "enter",
        ),
        (
            "http.request",
            {"http.method": "GET", "http.target": "/traced"},
            "exit",
        ),
    ]
