import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.core.health import HealthRegistry, HealthState, HealthStatus
from infra.plugins.observability import (
    ObservabilityService,
    install_observability_routes,
    render_prometheus_metrics,
)
from infra.plugins.services import OBSERVABILITY_SERVICE


class FakeHealth:
    def __init__(self, statuses: dict[str, HealthStatus]) -> None:
        self._statuses = statuses

    def snapshot(self) -> dict[str, HealthStatus]:
        return self._statuses


class FakeInfra:
    def __init__(
        self,
        statuses: dict[str, HealthStatus],
        observability: object | None = None,
        refresh_statuses: dict[str, HealthStatus] | None = None,
    ) -> None:
        self.health = FakeHealth(statuses)
        self._services = {"observability": observability} if observability else {}
        self._refresh_statuses = refresh_statuses
        self.refresh_calls = 0
        self.refresh_timeouts: list[float | None] = []

    def get(self, name: object, default: object | None = None) -> object | None:
        service_name = name.name if hasattr(name, "name") else name
        assert service_name == OBSERVABILITY_SERVICE.name
        return self._services.get(service_name, default)

    async def refresh_health(
        self,
        *,
        timeout_seconds: float | None = 5.0,
    ) -> dict[str, HealthStatus]:
        self.refresh_calls += 1
        self.refresh_timeouts.append(timeout_seconds)
        return (
            self._refresh_statuses if self._refresh_statuses is not None else self.health.snapshot()
        )


class FakeObservability:
    def __init__(self) -> None:
        self.counters = {"requests_total": 3}
        self.timers = {"request_seconds": [0.125, 0.375]}


class NotJsonSerializable:
    def __str__(self) -> str:
        return "fallback-detail"


def test_healthz_returns_health_snapshot_with_prefix():
    app = FastAPI()
    infra = FakeInfra(
        {
            "observability": HealthStatus(
                name="observability",
                status=HealthState.HEALTHY,
                message="ok",
            )
        }
    )
    install_observability_routes(app, infra, prefix="/ops")

    response = TestClient(app).get("/ops/healthz")

    assert response.status_code == 200
    assert infra.refresh_calls == 0
    assert response.json() == {
        "observability": {
            "name": "observability",
            "status": "healthy",
            "message": "ok",
            "details": {},
        }
    }


def test_healthz_converts_non_json_serializable_details_to_strings():
    app = FastAPI()
    infra = FakeInfra(
        {
            "observability": HealthStatus(
                name="observability",
                status=HealthState.HEALTHY,
                details={"raw": NotJsonSerializable()},
            )
        }
    )
    install_observability_routes(app, infra)

    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json()["observability"]["details"] == {"raw": "fallback-detail"}


def test_readyz_returns_503_when_any_status_is_unhealthy():
    app = FastAPI()
    infra = FakeInfra(
        {
            "database": HealthStatus(
                name="database",
                status=HealthState.UNHEALTHY,
                message="down",
            ),
            "cache": HealthStatus(name="cache", status=HealthState.DEGRADED),
        }
    )
    install_observability_routes(app, infra)

    response = TestClient(app).get("/readyz")

    assert response.status_code == 503
    assert response.json()["statuses"]["database"]["status"] == "unhealthy"
    assert response.json()["statuses"]["cache"]["status"] == "degraded"
    assert infra.refresh_calls == 1


def test_readyz_returns_200_when_no_status_is_unhealthy():
    app = FastAPI()
    infra = FakeInfra(
        {
            "database": HealthStatus(name="database", status=HealthState.HEALTHY),
            "cache": HealthStatus(name="cache", status=HealthState.DEGRADED),
        }
    )
    install_observability_routes(app, infra)

    response = TestClient(app).get("/readyz")

    assert response.status_code == 200
    assert response.json()["statuses"]["database"]["status"] == "healthy"
    assert response.json()["statuses"]["cache"]["status"] == "degraded"
    assert infra.refresh_calls == 1


def test_readyz_uses_refreshed_health_snapshot():
    app = FastAPI()
    infra = FakeInfra(
        {
            "database": HealthStatus(name="database", status=HealthState.HEALTHY),
        },
        refresh_statuses={
            "database": HealthStatus(
                name="database",
                status=HealthState.UNHEALTHY,
                message="connection lost",
            ),
        },
    )
    install_observability_routes(app, infra)

    response = TestClient(app).get("/readyz")

    assert response.status_code == 503
    assert infra.refresh_calls == 1
    assert response.json()["statuses"]["database"]["message"] == "connection lost"


def test_readyz_passes_configured_refresh_timeout():
    app = FastAPI()
    infra = FakeInfra(
        {
            "database": HealthStatus(name="database", status=HealthState.HEALTHY),
        }
    )
    install_observability_routes(app, infra, readiness_timeout_seconds=0.25)

    response = TestClient(app).get("/readyz")

    assert response.status_code == 200
    assert infra.refresh_timeouts == [0.25]


def test_install_observability_routes_rejects_negative_readiness_timeout():
    app = FastAPI()
    infra = FakeInfra({})

    with pytest.raises(ValueError, match="readiness_timeout_seconds"):
        install_observability_routes(app, infra, readiness_timeout_seconds=-1)


def test_metrics_exports_counters_and_timer_count_sum_as_text_plain():
    app = FastAPI()
    infra = FakeInfra({}, observability=FakeObservability())
    install_observability_routes(app, infra)

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert response.text.splitlines() == [
        "# TYPE requests_total counter",
        "requests_total 3",
        "# TYPE request_seconds summary",
        "request_seconds_count 2",
        "request_seconds_sum 0.5",
    ]


def test_metrics_can_use_prometheus_client_registry_when_configured():
    pytest.importorskip("prometheus_client")
    service = ObservabilityService(HealthRegistry(), metrics_backend="prometheus")
    service.increment("http_requests_total")
    service.timing("http_request_duration_seconds", 0.25)
    app = FastAPI()
    infra = FakeInfra({}, observability=service)
    install_observability_routes(app, infra)

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert "# TYPE http_requests_total counter" in response.text
    assert "http_requests_total 1.0" in response.text
    assert "# TYPE http_request_duration_seconds summary" in response.text
    assert "http_request_duration_seconds_count 1.0" in response.text
    assert "http_request_duration_seconds_sum 0.25" in response.text


def test_observability_service_can_use_opentelemetry_tracing_when_configured():
    pytest.importorskip("opentelemetry")
    service = ObservabilityService(HealthRegistry(), tracing_backend="opentelemetry")

    with service.span("test.span", {"component": "tests"}):
        service.event("inside-span")

    assert service.events[0].name == "inside-span"


def test_metrics_returns_empty_text_when_observability_service_is_missing():
    app = FastAPI()
    infra = FakeInfra({})
    install_observability_routes(app, infra)

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert response.text == ""


def test_metrics_sanitizes_invalid_metric_names_for_text_output():
    class UnsafeObservability:
        counters = {"bad metric\nname": 7, "9starts_with_digit": 2}
        timers = {"timer.with-dash": [1.25]}

    app = FastAPI()
    infra = FakeInfra({}, observability=UnsafeObservability())
    install_observability_routes(app, infra)

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.text.splitlines() == [
        "# TYPE _9starts_with_digit counter",
        "_9starts_with_digit 2",
        "# TYPE bad_metric_name counter",
        "bad_metric_name 7",
        "# TYPE timer_with_dash summary",
        "timer_with_dash_count 1",
        "timer_with_dash_sum 1.25",
    ]


def test_render_prometheus_metrics_is_stable_and_scrape_safe():
    class Metrics:
        counters = {"requests total": 2}
        timers = {"latency seconds": [0.1, 0.2]}

    assert render_prometheus_metrics(Metrics()).splitlines() == [
        "# TYPE requests_total counter",
        "requests_total 2",
        "# TYPE latency_seconds summary",
        "latency_seconds_count 2",
        "latency_seconds_sum 0.30000000000000004",
    ]


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
def test_install_observability_routes_raises_when_target_route_exists(path):
    app = FastAPI()

    @app.get(path)
    def existing_route():
        return {"ok": True}

    infra = FakeInfra({})

    try:
        install_observability_routes(app, infra)
    except RuntimeError as exc:
        assert path in str(exc)
    else:
        raise AssertionError("expected RuntimeError for duplicate observability route")


def test_install_observability_routes_raises_when_installed_twice():
    app = FastAPI()
    infra = FakeInfra({})
    install_observability_routes(app, infra)

    with pytest.raises(RuntimeError, match="/healthz"):
        install_observability_routes(app, infra)
