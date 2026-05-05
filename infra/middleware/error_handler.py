"""
统一错误处理策略

提供一致的错误处理机制,确保:
1. 错误传播策略一致
2. 日志记录标准化
3. 降级策略明确
4. 用户友好的错误信息

与新的异常体系集成:
- AppException: 应用基础异常
- DomainException: 领域/业务异常
- InfrastructureException: 基础设施异常
- IntegrationException: 外部集成异常
"""

from collections.abc import Callable
from enum import Enum
from functools import wraps
from typing import Any, TypeVar

from infra.exceptions import (
    AppException,
    DomainException,
    InfrastructureException,
    IntegrationException,
    RepositoryError,
    ValidationError,
)
from infra.logging import get_logger


logger = get_logger(__name__)

T = TypeVar("T")


class ErrorStrategy(str, Enum):
    """错误处理策略"""

    PROPAGATE = "propagate"  # 向上传播错误
    RETURN_NONE = "return_none"  # 返回None
    RETURN_EMPTY = "return_empty"  # 返回空集合([], {})
    RETURN_DEFAULT = "return_default"  # 返回默认值


class ErrorContext:
    """错误上下文"""

    def __init__(
        self,
        operation: str,
        strategy: ErrorStrategy = ErrorStrategy.PROPAGATE,
        default_value: Any = None,
        log_level: str = "error",
        user_message: str | None = None,
        alert: bool = False,
    ):
        """
        初始化错误上下文

        Args:
            operation: 操作描述
            strategy: 错误处理策略
            default_value: 默认返回值
            log_level: 日志级别 (debug/info/warning/error)
            user_message: 用户友好的错误信息
            alert: 是否需要告警
        """
        self.operation = operation
        self.strategy = strategy
        self.default_value = default_value
        self.log_level = log_level
        self.user_message = user_message
        self.alert = alert


def handle_error(context: ErrorContext):
    """
    错误处理装饰器

    支持新的异常体系，自动识别异常类型并采取适当的处理策略

    使用示例:

    ```python
    @handle_error(ErrorContext(
        operation="获取用户信息",
        strategy=ErrorStrategy.RETURN_NONE,
        user_message="无法获取用户信息,请稍后重试"
    ))
    async def get_user(user_id: int) -> Optional[User]:
        # 业务逻辑
        pass

    @handle_error(ErrorContext(
        operation="获取工具列表",
        strategy=ErrorStrategy.RETURN_EMPTY,
        default_value=[]
    ))
    async def list_tools() -> List[Tool]:
        # 业务逻辑
        pass
    ```
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                return await func(*args, **kwargs)

            except ValidationError as e:
                # 验证错误通常应该传播给调用方
                logger.warning(
                    f"{context.operation}失败 - 验证错误: {e.message}",
                    extra={
                        "operation": context.operation,
                        "error_type": "ValidationError",
                        "error_code": e.error_code,
                        "details": e.details,
                    },
                )
                raise

            except DomainException as e:
                # 领域/业务异常
                log_func = getattr(logger, context.log_level)
                log_func(
                    f"{context.operation}失败 - 业务异常: {e.message}",
                    extra={
                        "operation": context.operation,
                        "error_type": type(e).__name__,
                        "error_code": e.error_code,
                        "details": e.details,
                    },
                )
                return _handle_by_strategy(context, e)

            except RepositoryError as e:
                # Repository 异常
                log_func = logger.error
                log_func(
                    f"{context.operation}失败 - 数据访问异常: {e.message}",
                    extra={
                        "operation": context.operation,
                        "error_type": "RepositoryError",
                        "error_code": e.error_code,
                        "details": e.details,
                        "alert": True,
                    },
                )
                return _handle_by_strategy(context, e)

            except InfrastructureException as e:
                # 基础设施异常
                log_func = logger.error
                log_func(
                    f"{context.operation}失败 - 基础设施异常: {e.message}",
                    extra={
                        "operation": context.operation,
                        "error_type": type(e).__name__,
                        "error_code": e.error_code,
                        "details": e.details,
                        "alert": True,
                    },
                )
                return _handle_by_strategy(context, e)

            except IntegrationException as e:
                # 外部集成异常
                log_func = getattr(logger, context.log_level)
                log_func(
                    f"{context.operation}失败 - 外部服务异常: {e.message}",
                    extra={
                        "operation": context.operation,
                        "error_type": type(e).__name__,
                        "error_code": e.error_code,
                        "details": e.details,
                        "alert": context.alert,
                    },
                )
                return _handle_by_strategy(context, e)

            except AppException as e:
                # 其他应用异常
                log_func = getattr(logger, context.log_level)
                log_func(
                    f"{context.operation}失败 - 应用异常: {e.message}",
                    extra={
                        "operation": context.operation,
                        "error_type": type(e).__name__,
                        "error_code": e.error_code,
                        "details": e.details,
                        "alert": context.alert,
                    },
                )
                return _handle_by_strategy(context, e)

            except Exception as e:
                # 未预期的错误
                log_func = getattr(logger, context.log_level)
                log_func(
                    f"{context.operation}失败 - 未预期错误: {str(e)}",
                    exc_info=True,
                    extra={
                        "operation": context.operation,
                        "error_type": type(e).__name__,
                        "alert": context.alert,
                    },
                )
                return _handle_by_strategy(context, e)

        return wrapper

    return decorator


def _handle_by_strategy(context: ErrorContext, error: Exception) -> Any:
    """根据策略处理错误"""

    if context.strategy == ErrorStrategy.PROPAGATE:
        raise error

    elif context.strategy == ErrorStrategy.RETURN_NONE:
        return None

    elif context.strategy == ErrorStrategy.RETURN_EMPTY:
        # 尝试根据默认值推断返回类型
        if isinstance(context.default_value, list):
            return []
        elif isinstance(context.default_value, dict):
            return {}
        elif isinstance(context.default_value, tuple):
            return ()
        elif isinstance(context.default_value, set):
            return set()
        else:
            return context.default_value or []

    elif context.strategy == ErrorStrategy.RETURN_DEFAULT:
        return context.default_value

    else:
        raise error


class ErrorCategories:
    """预定义的错误处理上下文"""

    # 数据库查询错误 - 返回None
    DB_QUERY_ERROR = ErrorContext(
        operation="数据库查询",
        strategy=ErrorStrategy.RETURN_NONE,
        log_level="error",
        user_message="数据查询失败,请稍后重试",
    )

    # 列表查询错误 - 返回空列表
    LIST_QUERY_ERROR = ErrorContext(
        operation="列表查询",
        strategy=ErrorStrategy.RETURN_EMPTY,
        default_value=[],
        log_level="warning",
        user_message="无法获取列表数据",
    )

    # 缓存读取错误 - 降级处理,不影响主流程
    CACHE_ERROR = ErrorContext(
        operation="缓存操作",
        strategy=ErrorStrategy.RETURN_NONE,
        log_level="warning",
        alert=False,
    )

    # 外部API调用错误 - 需要告警
    EXTERNAL_API_ERROR = ErrorContext(
        operation="外部API调用",
        strategy=ErrorStrategy.PROPAGATE,
        log_level="error",
        user_message="外部服务暂时不可用,请稍后重试",
        alert=True,
    )

    # 工具执行错误 - 返回错误信息,但不中断流程
    TOOL_EXECUTION_ERROR = ErrorContext(
        operation="工具执行",
        strategy=ErrorStrategy.RETURN_DEFAULT,
        default_value={"success": False, "error": "工具执行失败"},
        log_level="warning",
    )

    # Repository 操作错误 - 传播异常
    REPOSITORY_ERROR = ErrorContext(
        operation="数据访问",
        strategy=ErrorStrategy.PROPAGATE,
        log_level="error",
        user_message="数据操作失败",
        alert=True,
    )


# ==================== 便捷装饰器 ====================


def handle_db_query_error(operation: str):
    """处理数据库查询错误"""
    return handle_error(
        ErrorContext(
            operation=operation, strategy=ErrorStrategy.RETURN_NONE, log_level="error"
        )
    )


def handle_list_query_error(operation: str, default_value=None):
    """处理列表查询错误"""
    return handle_error(
        ErrorContext(
            operation=operation,
            strategy=ErrorStrategy.RETURN_EMPTY,
            default_value=default_value or [],
            log_level="warning",
        )
    )


def handle_cache_error(operation: str):
    """处理缓存错误"""
    return handle_error(
        ErrorContext(
            operation=operation,
            strategy=ErrorStrategy.RETURN_NONE,
            log_level="warning",
            alert=False,
        )
    )


def handle_repository_error(operation: str):
    """处理 Repository 错误（传播异常）"""
    return handle_error(
        ErrorContext(
            operation=operation,
            strategy=ErrorStrategy.PROPAGATE,
            log_level="error",
            alert=True,
        )
    )


def handle_service_error(operation: str, default_value=None):
    """
    处理 Service 层错误

    可选择返回默认值或传播异常
    """
    strategy = (
        ErrorStrategy.RETURN_DEFAULT
        if default_value is not None
        else ErrorStrategy.PROPAGATE
    )
    return handle_error(
        ErrorContext(
            operation=operation,
            strategy=strategy,
            default_value=default_value,
            log_level="error",
        )
    )
