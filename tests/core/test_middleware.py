import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from infra.exceptions import AuthenticationError, ExternalServiceError
from infra.logging import get_trace_id
from infra.middleware import (
    REQUEST_ID_HEADER,
    TRACE_ID_HEADER,
    CORSMiddleware,
    ErrorHandlingMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
    install_error_handlers,
)


def test_request_logging_middleware_reuses_incoming_request_id_and_trace_id() -> None:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/ping")
    async def ping():
        return {"trace_id": get_trace_id()}

    response = TestClient(app).get(
        "/ping",
        headers={
            TRACE_ID_HEADER: "trace-from-gateway",
            REQUEST_ID_HEADER: "request-from-gateway",
        },
    )

    assert response.status_code == 200
    assert response.headers[TRACE_ID_HEADER] == "trace-from-gateway"
    assert response.headers[REQUEST_ID_HEADER] == "request-from-gateway"
    assert response.json() == {"trace_id": "trace-from-gateway"}


def test_request_logging_middleware_generates_request_id_when_missing() -> None:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    response = TestClient(app).get("/ping")

    assert response.status_code == 200
    assert response.headers[TRACE_ID_HEADER]
    assert response.headers[REQUEST_ID_HEADER] == response.headers[TRACE_ID_HEADER]


def test_error_handling_middleware_formats_app_exceptions_with_trace_headers() -> None:
    app = FastAPI()
    app.add_middleware(ErrorHandlingMiddleware)
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/private")
    async def private_route():
        raise AuthenticationError("token missing")

    response = TestClient(app).get(
        "/private",
        headers={TRACE_ID_HEADER: "trace-auth", REQUEST_ID_HEADER: "request-auth"},
    )

    body = response.json()
    assert response.status_code == 401
    assert response.headers[TRACE_ID_HEADER] == "trace-auth"
    assert response.headers[REQUEST_ID_HEADER] == "request-auth"
    assert body["success"] is False
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert body["error"]["message"] == "token missing"
    assert body["trace_id"] == "trace-auth"


def test_error_handling_middleware_keeps_trace_when_request_logging_is_inner() -> None:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(ErrorHandlingMiddleware)

    @app.get("/external")
    async def external_route():
        raise ExternalServiceError("billing", "unavailable")

    response = TestClient(app).get(
        "/external",
        headers={TRACE_ID_HEADER: "trace-external", REQUEST_ID_HEADER: "request-external"},
    )

    body = response.json()
    assert response.status_code == 502
    assert response.headers[TRACE_ID_HEADER] == "trace-external"
    assert response.headers[REQUEST_ID_HEADER] == "request-external"
    assert body["error"]["code"] == "EXTERNAL_SERVICE_ERROR"
    assert body["error"]["trace_id"] == "trace-external"


def test_error_handling_middleware_formats_unexpected_errors_without_debug_details() -> None:
    app = FastAPI(debug=False)
    app.add_middleware(ErrorHandlingMiddleware)
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internals")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/boom",
        headers={TRACE_ID_HEADER: "trace-boom"},
    )

    body = response.json()
    assert response.status_code == 500
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["message"] == "服务器内部错误"
    assert body["error"]["details"] is None
    assert body["trace_id"] == "trace-boom"


def test_error_handling_middleware_can_format_http_exceptions_from_inner_middleware() -> None:
    app = FastAPI()
    app.add_middleware(_RejectingMiddleware)
    app.add_middleware(ErrorHandlingMiddleware)

    @app.get("/blocked")
    async def blocked():
        return {"ok": True}

    response = TestClient(app, raise_server_exceptions=False).get(
        "/blocked",
        headers={TRACE_ID_HEADER: "trace-http"},
    )

    body = response.json()
    assert response.status_code == 404
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "missing"
    assert body["trace_id"] == "trace-http"


def test_install_error_handlers_formats_route_http_exception() -> None:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/missing")
    async def missing_route():
        raise HTTPException(status_code=404, detail={"reason": "not_found"})

    response = TestClient(app).get(
        "/missing",
        headers={TRACE_ID_HEADER: "trace-route-http", REQUEST_ID_HEADER: "request-route-http"},
    )

    body = response.json()
    assert response.status_code == 404
    assert response.headers[TRACE_ID_HEADER] == "trace-route-http"
    assert response.headers[REQUEST_ID_HEADER] == "request-route-http"
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "请求失败"
    assert body["error"]["details"] == {"detail": {"reason": "not_found"}}
    assert body["error"]["trace_id"] == "trace-route-http"


def test_install_error_handlers_formats_request_validation_error() -> None:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/items/{item_id}")
    async def read_item(item_id: int):
        return {"item_id": item_id}

    response = TestClient(app).get(
        "/items/not-an-int",
        headers={TRACE_ID_HEADER: "trace-validation"},
    )

    body = response.json()
    assert response.status_code == 422
    assert response.headers[TRACE_ID_HEADER] == "trace-validation"
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "请求参数校验失败"
    assert body["error"]["details"]["errors"][0]["loc"] == ["path", "item_id"]
    assert body["trace_id"] == "trace-validation"


def test_security_headers_middleware_sets_default_headers() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    response = TestClient(app).get("/ping")

    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "Strict-Transport-Security" not in response.headers


def test_security_headers_middleware_can_enable_hsts() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, hsts_max_age=31536000, hsts_preload=True)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    response = TestClient(app).get("/ping")

    assert (
        response.headers["Strict-Transport-Security"]
        == "max-age=31536000; includeSubDomains; preload"
    )


def test_security_headers_middleware_applies_to_formatted_errors() -> None:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/boom")
    async def boom():
        raise HTTPException(status_code=404, detail="missing")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_cors_middleware_allows_public_origins_without_credentials() -> None:
    app = FastAPI()
    app.add_middleware(CORSMiddleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    response = TestClient(app).get("/ping", headers={"Origin": "https://app.example"})

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in response.headers
    assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS"


def test_cors_middleware_skips_non_cors_requests() -> None:
    app = FastAPI()
    app.add_middleware(CORSMiddleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    response = TestClient(app).get("/ping")

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


def test_cors_middleware_preflight_for_explicit_origin_with_credentials() -> None:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://app.example"],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Trace-ID"],
        max_age=120,
    )

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    response = TestClient(app).options(
        "/ping",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.example"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert response.headers["Access-Control-Allow-Methods"] == "GET, POST"
    assert response.headers["Access-Control-Allow-Headers"] == "Authorization, Content-Type"
    assert response.headers["Access-Control-Expose-Headers"] == "X-Trace-ID"
    assert response.headers["Access-Control-Max-Age"] == "120"
    assert response.headers["Vary"] == "Origin"


def test_cors_middleware_rejects_wildcard_credentials() -> None:
    app = FastAPI()

    with pytest.raises(ValueError, match="allow_credentials"):
        app.add_middleware(CORSMiddleware, allow_credentials=True)
        TestClient(app).get("/ping")


def test_cors_middleware_ignores_disallowed_origin() -> None:
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["https://app.example"])

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    response = TestClient(app).get("/ping", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


def test_error_handling_middleware_is_public_api() -> None:
    import infra.middleware as middleware

    assert "ErrorHandlingMiddleware" in middleware.__all__
    assert "CORSMiddleware" in middleware.__all__
    assert "SecurityHeadersMiddleware" in middleware.__all__
    assert "install_error_handlers" in middleware.__all__
    assert middleware.ErrorHandlingMiddleware is ErrorHandlingMiddleware
    assert middleware.CORSMiddleware is CORSMiddleware
    assert middleware.SecurityHeadersMiddleware is SecurityHeadersMiddleware
    assert middleware.install_error_handlers is install_error_handlers


class _RejectingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raise HTTPException(status_code=404, detail="missing")
