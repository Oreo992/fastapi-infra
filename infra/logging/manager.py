"""
统一日志系统

结构化日志配置，支持分布式追踪
"""

import datetime
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from loguru import logger

# 上下文变量用于追踪
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
request_data_var: ContextVar[dict[str, Any] | None] = ContextVar("request_data", default=None)


def _context_patcher(record: Any) -> None:
    """自动将 trace_id/user_id 从 ContextVar 注入到每条日志的 extra 字段。"""
    record["extra"].setdefault("trace_id", trace_id_var.get(None) or "-")
    record["extra"].setdefault("request_id", request_id_var.get(None) or "-")
    record["extra"].setdefault("user_id", user_id_var.get(None) or "-")


def _make_rotation_check(max_bytes: int = 100 * 1024 * 1024):
    """创建组合轮转检查器：每日 OR 文件超过 max_bytes。

    文件大小检查每 100 条日志采样一次，避免每条日志都触发 stat 系统调用。
    """
    _current_date = [None]
    _write_count = [0]
    _check_interval = 100

    def check(message, file):
        # 跨天检查（轻量，仅比较日期对象）
        today = message.record["time"].date()
        if _current_date[0] is None:
            _current_date[0] = today
        if today != _current_date[0]:
            _current_date[0] = today
            _write_count[0] = 0
            return True

        # 文件大小检查（每 N 条采样一次）
        _write_count[0] += 1
        if _write_count[0] >= _check_interval:
            _write_count[0] = 0
            if os.path.getsize(os.path.abspath(file.name)) > max_bytes:
                return True

        return False

    return check


class _InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


class LoggerManager:
    """日志管理器

    使用方式：
        config = {
            "log_level": "INFO",
            "log_format": "pretty",  # 或 "json"
            "environment": "development",  # 或 "production"
        }
        manager = LoggerManager(config)
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._setup_logging()

    def _setup_logging(self) -> None:
        """设置日志系统"""
        logger.remove()
        logger.configure(patcher=_context_patcher)

        log_level = self._config.get("log_level", "INFO")
        environment = self._config.get("environment", "development")
        is_production = environment == "production"

        self._add_console_handler(
            log_level=log_level,
            is_production=is_production,
        )
        self._add_file_handlers(log_level=log_level, is_production=is_production)
        self._install_logging_intercept()
        self._set_dependency_log_levels()

    def _console_log_format(self, log_level: str) -> str:
        if self._config.get("log_format", "pretty") == "json":
            return (
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | "
                "{extra[trace_id]} | {name}:{function}:{line} | {message}"
            )
        log_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<yellow>{extra[trace_id]}</yellow> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "{message}"
        )
        if log_level == "DEBUG":
            log_format += " | <dim>{extra}</dim>"
        return log_format

    def _file_log_format(self) -> str:
        return (
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{extra[trace_id]} | {name}:{function}:{line} | {message}"
        )

    def _add_console_handler(self, *, log_level: str, is_production: bool) -> None:
        logger.add(
            sys.stderr,
            format=self._console_log_format(log_level),
            level=log_level,
            colorize=True,
            enqueue=True,
            backtrace=not is_production,
            diagnose=not is_production,
        )

    def _add_file_handlers(self, *, log_level: str, is_production: bool) -> None:
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        file_log_format = self._file_log_format()

        logger.add(
            logs_dir / "app.log",
            format=file_log_format,
            level=log_level,
            rotation=_make_rotation_check(100 * 1024 * 1024),
            retention="7 days",
            compression="zip",
            encoding="utf-8",
            enqueue=True,
            backtrace=not is_production,
            diagnose=not is_production,
        )

        logger.add(
            logs_dir / "error.log",
            format=file_log_format,
            level="ERROR",
            rotation=_make_rotation_check(100 * 1024 * 1024),
            retention="7 days",
            compression="zip",
            encoding="utf-8",
            enqueue=True,
            backtrace=not is_production,
            diagnose=not is_production,
        )

    def _install_logging_intercept(self) -> None:
        logging.getLogger().handlers.clear()
        logging.root.handlers = [_InterceptHandler()]
        logging.root.setLevel(0)

    def _set_dependency_log_levels(self) -> None:
        logging.getLogger("uvicorn").setLevel(logging.INFO)
        logging.getLogger("aiomysql").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def set_trace_id(self, trace_id: str | None = None) -> str:
        """设置追踪ID"""
        if not trace_id:
            trace_id = str(uuid.uuid4())
        trace_id_var.set(trace_id)
        return trace_id

    def set_user_id(self, user_id: str):
        """设置用户ID"""
        user_id_var.set(user_id)

    def set_request_data(self, data: dict[str, Any]):
        """设置请求数据"""
        request_data_var.set(data)

    def get_trace_id(self) -> str | None:
        """获取当前追踪ID"""
        return trace_id_var.get(None)

    def get_user_id(self) -> str | None:
        """获取当前用户ID"""
        return user_id_var.get(None)

    def get_request_data(self) -> dict[str, Any] | None:
        """获取当前请求数据"""
        return request_data_var.get(None)

    def reset_context(self):
        """重置所有上下文变量，避免内存泄漏"""
        trace_id_var.set(None)
        request_id_var.set(None)
        user_id_var.set(None)
        request_data_var.set(None)

    def clear_trace_id(self):
        """清除追踪ID"""
        trace_id_var.set(None)

    def set_request_id(self, request_id: str | None = None) -> str:
        """设置请求ID"""
        if not request_id:
            request_id = str(uuid.uuid4())
        request_id_var.set(request_id)
        return request_id

    def get_request_id(self) -> str | None:
        """获取当前请求ID"""
        return request_id_var.get(None)

    def clear_request_id(self):
        """清除请求ID"""
        request_id_var.set(None)

    def clear_user_id(self):
        """清除用户ID"""
        user_id_var.set(None)

    def clear_request_data(self):
        """清除请求数据"""
        request_data_var.set(None)

    @contextmanager
    def log_context(
        self,
        trace_id: str | None = None,
        user_id: str | None = None,
        request_data: dict[str, Any] | None = None,
    ):
        """上下文管理器，自动管理日志上下文的生命周期"""
        old_trace_token = None
        old_request_token = None
        old_user_token = None
        old_request_data_token = None

        try:
            if trace_id is not None:
                old_trace_token = trace_id_var.set(trace_id)
            request_id = request_data.get("request_id") if request_data is not None else None
            if isinstance(request_id, str):
                old_request_token = request_id_var.set(request_id)
            if user_id is not None:
                old_user_token = user_id_var.set(user_id)
            if request_data is not None:
                old_request_data_token = request_data_var.set(request_data)

            yield

        finally:
            if old_trace_token is not None:
                trace_id_var.reset(old_trace_token)
            if old_request_token is not None:
                request_id_var.reset(old_request_token)
            if old_user_token is not None:
                user_id_var.reset(old_user_token)
            if old_request_data_token is not None:
                request_data_var.reset(old_request_data_token)

    def get_current_context(self) -> dict[str, Any]:
        """获取当前所有上下文信息"""
        return {
            "trace_id": self.get_trace_id(),
            "request_id": self.get_request_id(),
            "user_id": self.get_user_id(),
            "request_data": self.get_request_data(),
        }


def get_logger(name: str | None = None) -> Any:
    """获取日志器实例。trace_id/user_id 由 patcher 自动注入。"""
    if name:
        return logger.bind(logger_name=name)
    return logger


def setup_logging(config: dict | None = None) -> LoggerManager:
    """显式配置日志系统并返回管理器实例。"""
    return LoggerManager(config)


def set_trace_id(trace_id: str | None = None) -> str:
    """设置追踪ID"""
    if not trace_id:
        trace_id = str(uuid.uuid4())
    trace_id_var.set(trace_id)
    return trace_id


def set_request_id(request_id: str | None = None) -> str:
    """设置请求ID"""
    if not request_id:
        request_id = str(uuid.uuid4())
    request_id_var.set(request_id)
    return request_id


def set_user_id(user_id: str) -> None:
    """设置用户ID"""
    user_id_var.set(user_id)


def set_log_context(
    trace_id: str | None = None,
    request_id: str | None = None,
    user_id: str | None = None,
    request_data: dict[str, Any] | None = None,
) -> None:
    """设置日志上下文"""
    if trace_id is not None:
        trace_id_var.set(trace_id)
    if request_id is not None:
        request_id_var.set(request_id)
    if user_id is not None:
        user_id_var.set(user_id)
    if request_data is not None:
        request_data_var.set(request_data)


def clear_log_context() -> None:
    """清除日志上下文，防止内存泄漏"""
    trace_id_var.set(None)
    request_id_var.set(None)
    user_id_var.set(None)
    request_data_var.set(None)


def get_trace_id() -> str | None:
    """获取当前追踪ID"""
    return trace_id_var.get(None)


def get_request_id() -> str | None:
    """获取当前请求ID"""
    return request_id_var.get(None)


def get_user_id() -> str | None:
    """获取当前用户ID"""
    return user_id_var.get(None)


@contextmanager
def log_context(
    trace_id: str | None = None,
    request_id: str | None = None,
    user_id: str | None = None,
    request_data: dict[str, Any] | None = None,
):
    """日志上下文管理器"""
    old_trace_token = None
    old_request_token = None
    old_user_token = None
    old_request_data_token = None

    try:
        if trace_id is not None:
            old_trace_token = trace_id_var.set(trace_id)
        if request_id is not None:
            old_request_token = request_id_var.set(request_id)
        if user_id is not None:
            old_user_token = user_id_var.set(user_id)
        if request_data is not None:
            old_request_data_token = request_data_var.set(request_data)
        yield
    finally:
        if old_trace_token is not None:
            trace_id_var.reset(old_trace_token)
        if old_request_token is not None:
            request_id_var.reset(old_request_token)
        if old_user_token is not None:
            user_id_var.reset(old_user_token)
        if old_request_data_token is not None:
            request_data_var.reset(old_request_data_token)
