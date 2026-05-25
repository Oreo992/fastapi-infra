from contextlib import contextmanager
from typing import Any

import pytest

from infra.http.client import (
    HttpClient,
    HttpError,
    HttpRetryConfig,
    MockHttpClient,
    _release_response,
)
from infra.logging import log_context


class SyncReleaseResponse:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class AsyncReleaseResponse:
    def __init__(self) -> None:
        self.released = False

    async def release(self) -> None:
        self.released = True


async def test_release_response_supports_sync_release() -> None:
    response = SyncReleaseResponse()

    await _release_response(response)

    assert response.released is True


async def test_release_response_supports_async_release() -> None:
    response = AsyncReleaseResponse()

    await _release_response(response)

    assert response.released is True


async def test_mock_http_client_records_request_and_returns_json_response() -> None:
    client = MockHttpClient(base_url="mock://upstream", body={"source": "mock"})

    response = await client.get("/items", headers={"X-Test": "yes"})

    assert response.status_code == 200
    assert response.url == "mock://upstream/items"
    assert response.json() == {
        "source": "mock",
        "request": {"method": "GET", "url": "mock://upstream/items"},
    }
    assert client.requests == [
        {
            "method": "GET",
            "url": "mock://upstream/items",
            "headers": {"X-Test": "yes"},
            "params": None,
            "json": None,
            "data": None,
        }
    ]


class FakeAiohttpResponse:
    def __init__(
        self,
        status: int = 200,
        text: str = '{"ok":true}',
        url: str = "https://api.example.test/items",
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self.url = url
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def read(self) -> bytes:
        return self._text.encode()

    async def text(self) -> str:
        return self._text


class FakeAiohttpSession:
    closed = False

    def __init__(self, outcomes: list[FakeAiohttpResponse | BaseException] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.outcomes = outcomes or [FakeAiohttpResponse()]

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeInstrumentation:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.timings: dict[str, list[float]] = {}
        self.spans: list[tuple[str, dict[str, str | int | float | bool] | None]] = []

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def timing(self, name: str, value: float) -> None:
        self.timings.setdefault(name, []).append(value)

    @contextmanager
    def span(self, name: str, attributes: dict[str, str | int | float | bool] | None = None):
        self.spans.append((name, attributes))
        yield


class FakeAiohttpModule:
    class ClientTimeout:
        def __init__(self, *, total: float) -> None:
            self.total = total


async def test_http_client_request_preserves_per_request_timeout(monkeypatch) -> None:
    client = HttpClient(base_url="https://api.example.test", timeout=30)
    session = FakeAiohttpSession()

    async def fake_ensure_session() -> None:
        client._session = session

    monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)
    monkeypatch.setattr("infra.http.client._load_aiohttp", lambda: FakeAiohttpModule)

    response = await client.get("/items", timeout=2.5)

    assert response.status_code == 200
    timeout = session.calls[0][2]["timeout"]
    assert getattr(timeout, "total") == 2.5


async def test_http_client_propagates_trace_headers_and_records_metrics(monkeypatch) -> None:
    instrumentation = FakeInstrumentation()
    client = HttpClient(
        base_url="https://api.example.test",
        instrumentation=instrumentation,
    )
    session = FakeAiohttpSession()

    async def fake_ensure_session() -> None:
        client._session = session

    monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)

    with log_context(trace_id="trace-123", request_id="request-456"):
        response = await client.get("/items")

    assert response.status_code == 200
    headers = session.calls[0][2]["headers"]
    assert headers["X-Trace-ID"] == "trace-123"
    assert headers["X-Request-ID"] == "request-456"
    assert instrumentation.counters["http_client_requests_total"] == 1
    assert instrumentation.counters["http_client_attempts_total"] == 1
    assert instrumentation.counters["http_client_responses_total"] == 1
    assert instrumentation.counters["http_client_status_200_total"] == 1
    assert "http_client_request_seconds" in instrumentation.timings
    assert instrumentation.spans == [
        (
            "http.client.request",
            {
                "http.method": "GET",
                "http.url": "https://api.example.test/items",
                "http.attempt": 1,
            },
        )
    ]


async def test_http_client_preserves_explicit_trace_headers(monkeypatch) -> None:
    client = HttpClient(base_url="https://api.example.test")
    session = FakeAiohttpSession()

    async def fake_ensure_session() -> None:
        client._session = session

    monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)

    with log_context(trace_id="trace-context", request_id="request-context"):
        await client.get(
            "/items",
            headers={"x-trace-id": "trace-explicit", "X-Request-ID": "request-explicit"},
        )

    headers = session.calls[0][2]["headers"]
    assert headers["x-trace-id"] == "trace-explicit"
    assert headers["X-Request-ID"] == "request-explicit"


async def test_http_client_retries_idempotent_retryable_status(monkeypatch) -> None:
    sleeps: list[float] = []
    instrumentation = FakeInstrumentation()
    client = HttpClient(
        base_url="https://api.example.test",
        retry_config=HttpRetryConfig(max_attempts=2, base_delay=0.5),
        retry_sleep=_record_sleep(sleeps),
        instrumentation=instrumentation,
    )
    session = FakeAiohttpSession(
        [
            FakeAiohttpResponse(status=503, text="temporary outage"),
            FakeAiohttpResponse(status=200, text='{"ok":true}'),
        ]
    )

    async def fake_ensure_session() -> None:
        client._session = session

    monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)

    response = await client.get("/items")

    assert response.status_code == 200
    assert len(session.calls) == 2
    assert sleeps == [0.5]
    assert instrumentation.counters["http_client_retries_total"] == 1
    assert instrumentation.counters["http_client_status_503_total"] == 1
    assert instrumentation.counters["http_client_status_200_total"] == 1


async def test_http_client_does_not_retry_post_by_default(monkeypatch) -> None:
    client = HttpClient(
        base_url="https://api.example.test",
        retry_config=HttpRetryConfig(max_attempts=2, base_delay=0),
    )
    session = FakeAiohttpSession(
        [
            FakeAiohttpResponse(status=503, text="temporary outage"),
            FakeAiohttpResponse(status=200, text='{"ok":true}'),
        ]
    )

    async def fake_ensure_session() -> None:
        client._session = session

    monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)

    response = await client.post("/items")

    assert response.status_code == 503
    assert len(session.calls) == 1


async def test_http_client_allows_explicit_post_retry(monkeypatch) -> None:
    client = HttpClient(base_url="https://api.example.test")
    session = FakeAiohttpSession(
        [
            FakeAiohttpResponse(status=429, text="rate limited"),
            FakeAiohttpResponse(status=200, text='{"ok":true}'),
        ]
    )

    async def fake_ensure_session() -> None:
        client._session = session

    monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)

    response = await client.post(
        "/items",
        retry_config=HttpRetryConfig(max_attempts=2, base_delay=0, retry_all_methods=True),
    )

    assert response.status_code == 200
    assert len(session.calls) == 2


async def test_http_client_does_not_retry_non_retryable_status(monkeypatch) -> None:
    client = HttpClient(
        base_url="https://api.example.test",
        retry_config=HttpRetryConfig(max_attempts=2, base_delay=0),
    )
    session = FakeAiohttpSession([FakeAiohttpResponse(status=404, text="missing")])

    async def fake_ensure_session() -> None:
        client._session = session

    monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)

    response = await client.get("/items")

    assert response.status_code == 404
    assert len(session.calls) == 1


async def test_http_client_retries_timeout_then_raises(monkeypatch) -> None:
    instrumentation = FakeInstrumentation()
    client = HttpClient(
        base_url="https://api.example.test",
        retry_config=HttpRetryConfig(max_attempts=2, base_delay=0),
        instrumentation=instrumentation,
    )
    session = FakeAiohttpSession([TimeoutError("slow"), TimeoutError("still slow")])

    async def fake_ensure_session() -> None:
        client._session = session

    monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)

    with pytest.raises(HttpError, match="请求超时"):
        await client.get("/items")

    assert len(session.calls) == 2
    assert instrumentation.counters["http_client_retries_total"] == 1
    assert instrumentation.counters["http_client_errors_total"] == 2


def test_http_retry_config_validates_attempts_and_delay() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        HttpRetryConfig(max_attempts=0)

    with pytest.raises(ValueError, match="base_delay"):
        HttpRetryConfig(base_delay=-0.1)


def _record_sleep(sleeps: list[float]):
    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    return sleep
