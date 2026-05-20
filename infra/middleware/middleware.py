"""
统一中间件

请求追踪、异常处理、API契约验证
"""

import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, ClassVar

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from infra.common.contracts import ApiResponse, ErrorCode
from infra.exceptions import (
    AppException,
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ExternalServiceError,
    PluginError,
)
from infra.logging import (
    clear_log_context,
    get_logger,
    get_trace_id,
    set_request_id,
    set_trace_id,
    set_user_id,
)

logger = get_logger(__name__)

TRACE_ID_HEADER = "X-Trace-ID"
REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"
DEFAULT_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"


def install_error_handlers(app: FastAPI) -> None:
    """Install API error handlers that FastAPI handles before middleware sees them."""

    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(AppException, _app_exception_handler)
    app.add_exception_handler(Exception, _unexpected_exception_handler)


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        return _unexpected_error_response(request, exc)
    return _http_exception_response(request, exc)


async def _validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        return _unexpected_error_response(request, exc)
    trace_id = _trace_id_for_request(request)
    request_id = _request_id_for_request(request, trace_id)
    logger.bind(
        type="validation_exception",
        status_code=422,
        trace_id=trace_id,
        request_id=request_id,
    ).warning("请求校验失败")

    response = JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            ApiResponse.fail(
                ErrorCode.VALIDATION_ERROR,
                "请求参数校验失败",
                details={"errors": exc.errors()},
                trace_id=trace_id,
            ).model_dump()
        ),
    )
    _attach_trace_headers(response, trace_id=trace_id, request_id=request_id)
    return response


async def _app_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppException):
        return _unexpected_error_response(request, exc)
    return _app_exception_response(request, exc)


async def _unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return _unexpected_error_response(request, exc)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """请求日志中间件"""

    def __init__(
        self,
        app,
        *,
        trace_id_header: str = TRACE_ID_HEADER,
        request_id_header: str = REQUEST_ID_HEADER,
        correlation_id_header: str = CORRELATION_ID_HEADER,
        include_request_body: bool = False,
        max_body_chars: int = 4096,
    ) -> None:
        super().__init__(app)
        self.trace_id_header = trace_id_header
        self.request_id_header = request_id_header
        self.correlation_id_header = correlation_id_header
        self.include_request_body = include_request_body
        self.max_body_chars = max_body_chars

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = set_trace_id(_incoming_trace_id(request, self.trace_id_header))
        request_id = _incoming_request_id(request, self.request_id_header) or trace_id
        set_request_id(request_id)
        correlation_id = _incoming_trace_id(request, self.correlation_id_header)
        request.state.trace_id = trace_id
        request.state.request_id = request_id
        if correlation_id is not None:
            request.state.correlation_id = correlation_id

        # 记录请求开始
        start_time = time.time()
        method = request.method
        path = str(request.url.path)

        # 提取用户ID（如果有）
        user_id = None
        if hasattr(request.state, "user_id"):
            user_id = request.state.user_id
            set_user_id(user_id)

        # 读取请求体
        request_body = None
        if self.include_request_body and method in ["POST", "PUT", "PATCH"]:
            try:
                body = await request.body()
                if body:
                    request_body = body.decode("utf-8")[: self.max_body_chars]
                    # 重新设置请求体，因为FastAPI只能读取一次
                    request._body = body
            except Exception as e:
                logger.warning(f"读取请求体失败: {e}")
                request_body = f"读取失败: {str(e)}"

        logger.bind(
            type="request_start",
            method=method,
            path=path,
            trace_id=trace_id,
            request_id=request_id,
            correlation_id=correlation_id,
            user_id=user_id,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
            request_body=request_body,
        ).info(f"请求开始: {method} {path}")

        try:
            response = await call_next(request)

            # 记录请求完成
            duration_ms = (time.time() - start_time) * 1000
            logger.bind(
                type="request_complete",
                method=method,
                path=path,
                status_code=response.status_code,
                duration_ms=duration_ms,
                trace_id=trace_id,
                request_id=request_id,
                correlation_id=correlation_id,
            ).info(f"请求完成: {method} {path} {response.status_code} ({duration_ms:.1f}ms)")

            _attach_trace_headers(
                response,
                trace_id=trace_id,
                request_id=request_id,
                correlation_id=correlation_id,
            )
            return response

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.bind(
                type="request_error",
                method=method,
                path=path,
                duration_ms=duration_ms,
                trace_id=trace_id,
                request_id=request_id,
                correlation_id=correlation_id,
                error=str(e),
            ).error(f"请求失败: {method} {path} ({duration_ms:.1f}ms)")
            raise
        finally:
            clear_log_context()


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """
    统一错误处理中间件

    支持基础设施异常体系：
    - AuthenticationError -> 401
    - AuthorizationError -> 403
    - ConfigurationError -> 500
    - PluginError -> 500
    - ExternalServiceError -> 502
    - AppException -> 500
    """

    # 异常类型到 HTTP 状态码的映射
    EXCEPTION_STATUS_MAP: ClassVar[dict[type[Exception], int]] = {
        AuthenticationError: 401,
        AuthorizationError: 403,
        ConfigurationError: 500,
        PluginError: 500,
        ExternalServiceError: 502,
        AppException: 500,
    }

    # 错误码映射
    ERROR_CODE_MAP: ClassVar[dict[type[Exception], ErrorCode]] = {
        AuthenticationError: ErrorCode.UNAUTHORIZED,
        AuthorizationError: ErrorCode.FORBIDDEN,
        ConfigurationError: ErrorCode.CONFIGURATION_ERROR,
        PluginError: ErrorCode.PLUGIN_ERROR,
        ExternalServiceError: ErrorCode.EXTERNAL_SERVICE_ERROR,
    }

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        try:
            return await call_next(request)
        except HTTPException as e:
            return self._handle_http_exception(e, request)
        except AppException as e:
            # 新的统一异常体系
            return self._handle_app_exception(e, request)
        except Exception as e:
            # 未预期错误
            return self._handle_unexpected_error(e, request)

    def _handle_http_exception(self, e: HTTPException, request: Request) -> JSONResponse:
        return _http_exception_response(request, e)

    def _handle_app_exception(self, e: AppException, request: Request) -> JSONResponse:
        """处理 AppException 及其子类"""
        return _app_exception_response(request, e)

    def _handle_unexpected_error(self, e: Exception, request: Request) -> JSONResponse:
        """处理未预期的异常"""
        return _unexpected_error_response(request, e)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add conservative security headers to every HTTP response."""

    def __init__(
        self,
        app,
        *,
        frame_options: str = "DENY",
        content_type_options: str = "nosniff",
        referrer_policy: str = "no-referrer",
        permissions_policy: str | None = DEFAULT_PERMISSIONS_POLICY,
        cross_origin_opener_policy: str | None = "same-origin",
        hsts_max_age: int | None = None,
        hsts_include_subdomains: bool = True,
        hsts_preload: bool = False,
    ) -> None:
        super().__init__(app)
        self.frame_options = frame_options
        self.content_type_options = content_type_options
        self.referrer_policy = referrer_policy
        self.permissions_policy = permissions_policy
        self.cross_origin_opener_policy = cross_origin_opener_policy
        self.hsts_max_age = hsts_max_age
        self.hsts_include_subdomains = hsts_include_subdomains
        self.hsts_preload = hsts_preload

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        self.apply(response)
        return response

    def apply(self, response: Response) -> None:
        response.headers.setdefault("X-Frame-Options", self.frame_options)
        response.headers.setdefault("X-Content-Type-Options", self.content_type_options)
        response.headers.setdefault("Referrer-Policy", self.referrer_policy)
        if self.permissions_policy is not None:
            response.headers.setdefault("Permissions-Policy", self.permissions_policy)
        if self.cross_origin_opener_policy is not None:
            response.headers.setdefault(
                "Cross-Origin-Opener-Policy",
                self.cross_origin_opener_policy,
            )
        if self.hsts_max_age is not None:
            response.headers.setdefault("Strict-Transport-Security", self._hsts_header())

    def _hsts_header(self) -> str:
        if self.hsts_max_age is None:
            raise RuntimeError("hsts_max_age is not configured")
        value = f"max-age={self.hsts_max_age}"
        if self.hsts_include_subdomains:
            value += "; includeSubDomains"
        if self.hsts_preload:
            value += "; preload"
        return value


def _http_exception_response(request: Request, exc: HTTPException) -> JSONResponse:
    trace_id = _trace_id_for_request(request)
    request_id = _request_id_for_request(request, trace_id)
    error_code = _error_code_for_status(exc.status_code)
    message = _http_exception_message(exc.detail)

    logger.bind(
        type="http_exception",
        status_code=exc.status_code,
        error_code=error_code.value,
        trace_id=trace_id,
        request_id=request_id,
    ).warning(f"HTTP异常: {exc.status_code} {message}")

    response = JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(
            ApiResponse.fail(
                error_code,
                message,
                details=_http_exception_details(exc.detail),
                trace_id=trace_id,
            ).model_dump()
        ),
        headers=exc.headers,
    )
    _attach_trace_headers(response, trace_id=trace_id, request_id=request_id)
    return response


def _app_exception_response(request: Request, exc: AppException) -> JSONResponse:
    trace_id = _trace_id_for_request(request)
    request_id = _request_id_for_request(request, trace_id)

    status_code = 500
    for exc_type, status_value in ErrorHandlingMiddleware.EXCEPTION_STATUS_MAP.items():
        if isinstance(exc, exc_type):
            status_code = status_value
            break

    error_code = ErrorCode.INTERNAL_ERROR
    for exc_type, error_value in ErrorHandlingMiddleware.ERROR_CODE_MAP.items():
        if isinstance(exc, exc_type):
            error_code = error_value
            break

    if exc.error_code:
        try:
            error_code = ErrorCode(exc.error_code)
        except ValueError:
            pass

    log_level = "warning" if status_code < 500 else "error"
    getattr(logger, log_level)(
        f"应用异常: {exc.message}",
        extra={
            "error_type": type(exc).__name__,
            "error_code": exc.error_code,
            "details": exc.details,
            "trace_id": trace_id,
            "request_id": request_id,
        },
        exc_info=status_code >= 500,
    )

    response = JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            ApiResponse.fail(
                error_code,
                exc.message,
                details=exc.details,
                trace_id=trace_id,
            ).model_dump()
        ),
    )
    _attach_trace_headers(response, trace_id=trace_id, request_id=request_id)
    return response


def _unexpected_error_response(request: Request, exc: Exception) -> JSONResponse:
    trace_id = _trace_id_for_request(request)
    request_id = _request_id_for_request(request, trace_id)
    error_msg = str(exc)

    logger.bind(
        type="unhandled_error",
        error=error_msg,
        trace_id=trace_id,
        request_id=request_id,
    ).error("未处理的异常", exc_info=True)

    details = None
    if hasattr(request.app, "debug") and request.app.debug:
        details = {"original_error": error_msg}

    response = JSONResponse(
        status_code=500,
        content=jsonable_encoder(
            ApiResponse.fail(
                ErrorCode.INTERNAL_ERROR,
                "服务器内部错误",
                details=details,
                trace_id=trace_id,
            ).model_dump()
        ),
    )
    _attach_trace_headers(response, trace_id=trace_id, request_id=request_id)
    return response


def _http_exception_message(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if detail is None:
        return "请求失败"
    return "请求失败"


def _http_exception_details(detail: Any) -> dict[str, Any] | None:
    if isinstance(detail, str) or detail is None:
        return None
    return {"detail": detail}


def _incoming_trace_id(request: Request, header_name: str) -> str | None:
    value = request.headers.get(header_name)
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return value[:128]


def _incoming_request_id(request: Request, header_name: str) -> str | None:
    return _incoming_trace_id(request, header_name)


def _trace_id_for_request(request: Request) -> str:
    existing = get_trace_id()
    if existing:
        return existing
    state_trace_id = getattr(request.state, "trace_id", None)
    if isinstance(state_trace_id, str) and state_trace_id:
        return state_trace_id
    return set_trace_id(_incoming_trace_id(request, TRACE_ID_HEADER))


def _request_id_for_request(request: Request, trace_id: str) -> str:
    state_request_id = getattr(request.state, "request_id", None)
    if isinstance(state_request_id, str) and state_request_id:
        return state_request_id
    return _incoming_request_id(request, REQUEST_ID_HEADER) or trace_id


def _attach_trace_headers(
    response: Response,
    *,
    trace_id: str,
    request_id: str,
    correlation_id: str | None = None,
) -> None:
    response.headers[TRACE_ID_HEADER] = trace_id
    response.headers[REQUEST_ID_HEADER] = request_id
    if correlation_id is not None:
        response.headers[CORRELATION_ID_HEADER] = correlation_id


def _error_code_for_status(status_code: int) -> ErrorCode:
    if status_code == 401:
        return ErrorCode.UNAUTHORIZED
    if status_code == 403:
        return ErrorCode.FORBIDDEN
    if status_code == 404:
        return ErrorCode.NOT_FOUND
    if status_code == 409:
        return ErrorCode.CONFLICT
    if status_code == 422:
        return ErrorCode.VALIDATION_ERROR
    if status_code == 429:
        return ErrorCode.TOO_MANY_REQUESTS
    return ErrorCode.INTERNAL_ERROR if status_code >= 500 else ErrorCode.VALIDATION_ERROR


class CORSMiddleware(BaseHTTPMiddleware):
    """Minimal CORS middleware with safe credential defaults."""

    def __init__(
        self,
        app,
        *,
        allow_origins: Iterable[str] | None = None,
        allow_methods: Iterable[str] | None = None,
        allow_headers: Iterable[str] | None = None,
        expose_headers: Iterable[str] | None = None,
        allow_credentials: bool = False,
        max_age: int = 600,
    ) -> None:
        super().__init__(app)
        self.allow_origins = tuple(allow_origins or ("*",))
        self.allow_methods = tuple(allow_methods or ("GET", "POST", "PUT", "DELETE", "OPTIONS"))
        self.allow_headers = tuple(allow_headers or ("*",))
        self.expose_headers = tuple(expose_headers or ())
        self.allow_credentials = allow_credentials
        self.max_age = max_age

        if self.allow_credentials and "*" in self.allow_origins:
            raise ValueError("allow_credentials=True requires explicit allow_origins")

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        is_preflight = (
            request.method == "OPTIONS"
            and request.headers.get("origin") is not None
            and request.headers.get("access-control-request-method") is not None
        )
        if is_preflight:
            response = Response(status_code=204)
        else:
            response = await call_next(request)

        origin = request.headers.get("origin")
        allowed_origin = self._allowed_origin(origin)
        if allowed_origin is None:
            return response

        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods)
        response.headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers)
        response.headers["Access-Control-Max-Age"] = str(self.max_age)
        if self.expose_headers:
            response.headers["Access-Control-Expose-Headers"] = ", ".join(self.expose_headers)
        if self.allow_credentials:
            response.headers["Access-Control-Allow-Credentials"] = "true"
        if allowed_origin != "*":
            response.headers.add_vary_header("Origin")

        return response

    def _allowed_origin(self, origin: str | None) -> str | None:
        if origin is None:
            return None
        if "*" in self.allow_origins:
            return "*"
        if origin in self.allow_origins:
            return origin
        return None
