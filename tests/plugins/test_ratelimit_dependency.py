from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from infra.middleware import install_error_handlers
from infra.plugins import RATELIMIT_SERVICE
from infra.plugins.ratelimit import MemoryRateLimiter, rate_limit


class FakeInfra:
    def __init__(self, limiter: MemoryRateLimiter) -> None:
        self.limiter = limiter

    def get(self, service):
        service_name = service.name if hasattr(service, "name") else service
        assert service_name == RATELIMIT_SERVICE.name
        return self.limiter


def test_rate_limit_dependency_blocks_after_limit() -> None:
    app = FastAPI()
    install_error_handlers(app)
    app.state.infra = FakeInfra(MemoryRateLimiter())

    @app.get("/limited", dependencies=[Depends(rate_limit(limit=1, window_seconds=60))])
    async def limited():
        return {"ok": True}

    client = TestClient(app)

    first = client.get("/limited")
    second = client.get("/limited")

    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "1"
    assert first.headers["X-RateLimit-Window"] == "60"
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"
    assert second.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert second.json()["error"]["message"] == "rate limit exceeded"


def test_rate_limit_dependency_accepts_custom_key_func() -> None:
    app = FastAPI()
    install_error_handlers(app)
    app.state.infra = FakeInfra(MemoryRateLimiter())

    def account_key(request: Request) -> str:
        return f"account:{request.headers['X-Account-ID']}"

    @app.get(
        "/limited",
        dependencies=[Depends(rate_limit(limit=1, window_seconds=60, key_func=account_key))],
    )
    async def limited():
        return {"ok": True}

    client = TestClient(app)

    first_a = client.get("/limited", headers={"X-Account-ID": "a"})
    first_b = client.get("/limited", headers={"X-Account-ID": "b"})
    second_a = client.get("/limited", headers={"X-Account-ID": "a"})

    assert first_a.status_code == 200
    assert first_b.status_code == 200
    assert second_a.status_code == 429


def test_rate_limit_dependency_accepts_service_key() -> None:
    app = FastAPI()
    install_error_handlers(app)
    app.state.infra = FakeInfra(MemoryRateLimiter())

    @app.get(
        "/limited",
        dependencies=[
            Depends(
                rate_limit(
                    limit=1,
                    window_seconds=60,
                    service=RATELIMIT_SERVICE,
                )
            )
        ],
    )
    async def limited():
        return {"ok": True}

    response = TestClient(app).get("/limited")

    assert response.status_code == 200


def test_rate_limit_dependency_validates_factory_arguments() -> None:
    import pytest

    with pytest.raises(ValueError, match="limit"):
        rate_limit(limit=0, window_seconds=60)

    with pytest.raises(ValueError, match="window_seconds"):
        rate_limit(limit=1, window_seconds=0)

    with pytest.raises(ValueError, match="service"):
        rate_limit(limit=1, window_seconds=60, service=" ")
