"""中间件模块

提供企业级中间件组件：
- RequestLoggingMiddleware: 请求日志追踪
- ErrorHandlingMiddleware: 统一错误响应
- ErrorStrategy: 错误处理策略
- ErrorContext: 错误上下文
- handle_error: 错误处理函数
"""

from infra.middleware.error_handler import (
    ErrorContext,
    ErrorStrategy,
    handle_error,
)
from infra.middleware.middleware import (
    CORRELATION_ID_HEADER,
    DEFAULT_PERMISSIONS_POLICY,
    REQUEST_ID_HEADER,
    TRACE_ID_HEADER,
    CORSMiddleware,
    ErrorHandlingMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
    install_error_handlers,
)

__all__ = [
    "RequestLoggingMiddleware",
    "ErrorHandlingMiddleware",
    "TRACE_ID_HEADER",
    "REQUEST_ID_HEADER",
    "CORRELATION_ID_HEADER",
    "DEFAULT_PERMISSIONS_POLICY",
    "install_error_handlers",
    "CORSMiddleware",
    "SecurityHeadersMiddleware",
    "ErrorStrategy",
    "ErrorContext",
    "handle_error",
]
