import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.core.health import HealthState, HealthStatus
from infra.plugins.observability import install_observability_routes


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
    ) -> None:
        self.health = FakeHealth(statuses)
        self._services = {"observability": observability} if observability else {}

    def get(self, name: str, default: object | None = None) -> object | None:
        return self._services.get(name, default)


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
    assert response.json()["observability"]["details"] == {
        "raw": "fallback-detail"
    }


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


def test_metrics_exports_counters_and_timer_count_sum_as_text_plain():
    app = FastAPI()
    infra = FakeInfra({}, observability=FakeObservability())
    install_observability_routes(app, infra)

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.splitlines() == [
        "requests_total 3",
        "request_seconds_count 2",
        "request_seconds_sum 0.5",
    ]


def test_metrics_returns_empty_text_when_observability_service_is_missing():
    app = FastAPI()
    infra = FakeInfra({})
    install_observability_routes(app, infra)

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
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
        "bad_metric_name 7",
        "_9starts_with_digit 2",
        "timer_with_dash_count 1",
        "timer_with_dash_sum 1.25",
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
