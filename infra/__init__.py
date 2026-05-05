"""
FastAPI Infrastructure Package

高性能、生产级的 FastAPI 基础设施包
"""

__version__ = "0.1.0"

# 核心基础设施
from infra.http import HttpClient, HttpResponse
from infra.database import DatabaseManager, BaseRepository
from infra.cache import CacheService
from infra.logging import get_logger, LoggerManager
from infra.registry import ServiceRegistry, ServiceContainer
from infra.config import BaseSettings
from infra.exceptions import AppException, RepositoryError

# 中间件
from infra.middleware import RequestLoggingMiddleware, ErrorStrategy

# 并发控制
from infra.concurrency import GlobalThreadPoolManager

# API 契约
from infra.common import ApiResponse, ErrorCode, ErrorDetail

# 流式响应
from infra.streaming import StreamsManager, StreamMessage

# 生命周期管理
from infra.startup import LifecycleManager, create_lifecycle_manager

__all__ = [
    # 核心
    "HttpClient",
    "HttpResponse",
    "DatabaseManager",
    "BaseRepository",
    "CacheService",
    "get_logger",
    "LoggerManager",
    "ServiceRegistry",
    "ServiceContainer",
    "BaseSettings",
    "AppException",
    "RepositoryError",
    # 中间件
    "RequestLoggingMiddleware",
    "ErrorStrategy",
    # 并发
    "GlobalThreadPoolManager",
    # API
    "ApiResponse",
    "ErrorCode",
    "ErrorDetail",
    # 流式
    "StreamsManager",
    "StreamMessage",
    # 生命周期
    "LifecycleManager",
    "create_lifecycle_manager",
]
