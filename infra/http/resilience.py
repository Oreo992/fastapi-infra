"""
服务弹性和容错机制

包含重试和超时管理机制
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from infra.logging import get_logger

logger = get_logger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


@dataclass
class RetryConfig:
    """重试配置"""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True


@dataclass
class TimeoutConfig:
    """超时配置"""

    timeout_seconds: float = 60


class RetryMechanism:
    """重试机制实现"""

    def __init__(self, config: RetryConfig):
        self.config = config

    async def execute(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """执行函数并应用重试逻辑"""
        last_exception: Exception | None = None

        for attempt in range(self.config.max_attempts):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt == self.config.max_attempts - 1:
                    extra = {
                        "function_name": func.__name__,
                        "attempt": attempt,
                        "max_attempts": self.config.max_attempts,
                        "exception_type": type(e).__name__,
                        "exception_message": str(e),
                        # 避免记录不可序列化的参数对象，仅记录kwargs键名
                        "kwargs_keys": list(kwargs.keys()) if kwargs else None,
                    }
                    logger.opt(exception=e).error(
                        f"重试失败，已达到最大尝试次数 {self.config.max_attempts}",
                        extra=extra,
                    )
                    break

                delay = self._calculate_delay(attempt)
                extra = {
                    "function_name": func.__name__,
                    "attempt": attempt,
                    "max_attempts": self.config.max_attempts,
                    "exception_type": type(e).__name__,
                    "exception_message": str(e),
                    "retry_delay": delay,
                }
                logger.warning(
                    f"第{attempt}次尝试失败，{delay:.2f}秒后重试",
                    extra=extra,
                )
                await asyncio.sleep(delay)

        if last_exception is None:
            raise RuntimeError("retry configuration must allow at least one attempt")
        raise last_exception

    def _calculate_delay(self, attempt: int) -> float:
        """计算延迟时间"""
        delay = self.config.base_delay * (self.config.exponential_base**attempt)
        delay = min(delay, self.config.max_delay)

        # 添加抖动
        if self.config.jitter:
            import random

            delay = delay * (0.5 + random.random() * 0.5)

        return delay


class TimeoutManager:
    """超时管理器"""

    def __init__(self, config: TimeoutConfig):
        self.config = config

    async def execute(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """执行函数并应用超时控制"""
        try:
            return await asyncio.wait_for(
                func(*args, **kwargs), timeout=self.config.timeout_seconds
            )
        except TimeoutError as e:
            logger.error(
                f"函数执行超时 ({self.config.timeout_seconds}s)",
                extra={
                    "function_name": func.__name__,
                    "timeout_seconds": self.config.timeout_seconds,
                    # 不记录原始args以避免多进程日志序列化失败
                    "kwargs_keys": list(kwargs.keys()) if kwargs else None,
                },
                exc_info=True,
            )
            raise Exception(f"操作超时 ({self.config.timeout_seconds}s)") from e


def with_resilience(
    retry_config: RetryConfig | None = None,
    timeout_config: TimeoutConfig | None = None,
    service_name: str | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Coroutine[Any, Any, T]]]:
    """弹性装饰器 - 支持重试和超时"""

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
        retry_mechanism = RetryMechanism(retry_config) if retry_config else None
        timeout_manager = TimeoutManager(timeout_config) if timeout_config else None

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            func_name = service_name or func.__name__

            async def execute() -> T:
                # 超时控制
                if timeout_manager:
                    return await timeout_manager.execute(func, *args, **kwargs)
                else:
                    return await func(*args, **kwargs)

            # 重试机制
            if retry_mechanism:
                return await retry_mechanism.execute(execute)
            else:
                return await execute()

        return wrapper

    return decorator


# 预定义配置
class PresetConfigs:
    """预设配置"""

    # 外部服务配置
    EXTERNAL_SERVICE_RETRY = RetryConfig(
        max_attempts=3,
        base_delay=2.0,
        max_delay=30.0,
        exponential_base=2.0,
        jitter=True,
    )

    EXTERNAL_SERVICE_TIMEOUT = TimeoutConfig(timeout_seconds=100.0)

    # 数据库配置
    DB_RETRY = RetryConfig(max_attempts=2, base_delay=0.5, max_delay=5.0)

    DB_TIMEOUT = TimeoutConfig(timeout_seconds=30.0)  # 增加到30秒，适应更复杂的查询

    # API调用配置
    API_RETRY = RetryConfig(max_attempts=3, base_delay=1.0, max_delay=10.0)

    API_TIMEOUT = TimeoutConfig(timeout_seconds=15.0)

    # HTTP请求配置
    HTTP_RETRY = RetryConfig(
        max_attempts=3,
        base_delay=1.5,
        max_delay=15.0,
        exponential_base=2.0,
        jitter=True,
    )

    HTTP_TIMEOUT = TimeoutConfig(timeout_seconds=30.0)
