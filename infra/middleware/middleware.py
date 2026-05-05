"""
统一中间件

请求追踪、异常处理、API契约验证
"""

import time
from collections.abc import Callable

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from infra.common.contracts import ApiResponse, ErrorCode, ErrorDetail
from infra.exceptions import (
    AppException,
    DomainException,
    EntityNotFoundError,
    InfrastructureException,
    IntegrationException,
    RepositoryError,
    ValidationError,
)
from infra.logging import get_logger, log_manager
from infra.utils import get_timestamp


logger = get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """请求日志中间件"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 生成追踪ID
        trace_id = log_manager.set_trace_id()

        # 记录请求开始
        start_time = time.time()
        method = request.method
        path = str(request.url.path)

        # 提取用户ID（如果有）
        user_id = None
        if hasattr(request.state, "user_id"):
            user_id = request.state.user_id
            log_manager.set_user_id(user_id)

        # 读取请求体
        request_body = None
        if method in ["POST", "PUT", "PATCH"]:
            try:
                body = await request.body()
                if body:
                    request_body = body.decode("utf-8")
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
            ).info(
                f"请求完成: {method} {path} {response.status_code} ({duration_ms:.1f}ms)"
            )

            # 添加追踪ID到响应头
            response.headers["X-Trace-ID"] = trace_id
            return response

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.bind(
                type="request_error",
                method=method,
                path=path,
                duration_ms=duration_ms,
                trace_id=trace_id,
                error=str(e),
            ).error(f"请求失败: {method} {path} ({duration_ms:.1f}ms)")
            raise


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """
    统一错误处理中间件

    支持新的异常体系：
    - AppException (基础异常)
      - DomainException (领域异常) -> 400
        - EntityNotFoundError -> 404
        - ValidationError -> 422
        - BusinessRuleViolationError -> 400
      - InfrastructureException (基础设施异常) -> 500
        - RepositoryError -> 500
        - DatabaseError -> 500
        - CacheError -> 500
      - IntegrationException (外部集成异常) -> 502
        - ExternalServiceError -> 502
        - LLMServiceError -> 502
    """

    # 异常类型到 HTTP 状态码的映射
    EXCEPTION_STATUS_MAP = {
        EntityNotFoundError: 404,
        ValidationError: 422,
        DomainException: 400,
        RepositoryError: 500,
        InfrastructureException: 500,
        IntegrationException: 502,
        AppException: 500,
    }

    # 错误码映射
    ERROR_CODE_MAP = {
        EntityNotFoundError: ErrorCode.NOT_FOUND,
        ValidationError: ErrorCode.VALIDATION_ERROR,
        RepositoryError: ErrorCode.DATABASE_ERROR,
        IntegrationException: ErrorCode.EXTERNAL_API_ERROR,
    }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)
        except HTTPException:
            # FastAPI 的 HTTPException 由 FastAPI 处理
            raise
        except AppException as e:
            # 新的统一异常体系
            return self._handle_app_exception(e, request)
        except Exception as e:
            # 未预期错误
            return self._handle_unexpected_error(e, request)

    def _handle_app_exception(self, e: AppException, request: Request) -> JSONResponse:
        """处理 AppException 及其子类"""
        trace_id = log_manager.get_trace_id()

        # 确定 HTTP 状态码
        status_code = 500
        for exc_type, code in self.EXCEPTION_STATUS_MAP.items():
            if isinstance(e, exc_type):
                status_code = code
                break

        # 确定错误码
        error_code = ErrorCode.INTERNAL_ERROR
        for exc_type, code in self.ERROR_CODE_MAP.items():
            if isinstance(e, exc_type):
                error_code = code
                break

        # 尝试从异常中获取错误码
        if hasattr(e, "error_code") and e.error_code:
            try:
                error_code = ErrorCode(e.error_code)
            except ValueError:
                # 如果错误码不在枚举中，使用默认值
                pass

        # 记录日志
        log_level = "warning" if status_code < 500 else "error"
        getattr(logger, log_level)(
            f"业务异常: {e.message}",
            extra={
                "error_type": type(e).__name__,
                "error_code": e.error_code,
                "details": e.details,
                "trace_id": trace_id,
            },
            exc_info=status_code >= 500,
        )

        return JSONResponse(
            status_code=status_code,
            content=ApiResponse(
                success=False,
                error=ErrorDetail(
                    code=error_code,
                    message=e.message,
                    details=e.details,
                    trace_id=trace_id,
                ),
                timestamp=get_timestamp(),
                trace_id=trace_id,
            ).model_dump(),
        )

    def _handle_unexpected_error(self, e: Exception, request: Request) -> JSONResponse:
        """处理未预期的异常"""
        trace_id = log_manager.get_trace_id()
        error_msg = str(e)

        logger.bind(
            type="unhandled_error",
            error=error_msg,
            trace_id=trace_id,
        ).error("未处理的异常", exc_info=True)

        # 在调试模式下返回详细错误信息
        details = None
        if hasattr(request.app, "debug") and request.app.debug:
            details = {"original_error": error_msg}

        return JSONResponse(
            status_code=500,
            content=ApiResponse(
                success=False,
                error=ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="服务器内部错误",
                    details=details,
                    trace_id=trace_id,
                ),
                timestamp=get_timestamp(),
                trace_id=trace_id,
            ).model_dump(),
        )


class CORSMiddleware(BaseHTTPMiddleware):
    """统一CORS处理"""

    def __init__(self, app, allow_origins=None, allow_methods=None, allow_headers=None):
        super().__init__(app)
        self.allow_origins = allow_origins or ["*"]
        self.allow_methods = allow_methods or [
            "GET",
            "POST",
            "PUT",
            "DELETE",
            "OPTIONS",
        ]
        self.allow_headers = allow_headers or ["*"]

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method == "OPTIONS":
            response = Response()
        else:
            response = await call_next(request)

        origin = request.headers.get("origin")
        if origin and ("*" in self.allow_origins or origin in self.allow_origins):
            response.headers["Access-Control-Allow-Origin"] = origin
        elif "*" in self.allow_origins:
            response.headers["Access-Control-Allow-Origin"] = "*"

        response.headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods)
        response.headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers)
        response.headers["Access-Control-Allow-Credentials"] = "true"

        return response
