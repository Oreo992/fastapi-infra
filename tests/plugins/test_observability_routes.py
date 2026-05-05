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
