"""中间件模块

提供企业级中间件组件：
- RequestLoggingMiddleware: 请求日志追踪
- ErrorStrategy: 错误处理策略
- ErrorContext: 错误上下文
- handle_error: 错误处理函数
"""

from infra.middleware.middleware import RequestLoggingMiddleware
from infra.middleware.error_handler import (
    ErrorStrategy,
    ErrorContext,
    handle_error,
)

__all__ = [
    "RequestLoggingMiddleware",
    "ErrorStrategy",
    "ErrorContext",
    "handle_error",
]
