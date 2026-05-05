"""
统一API契约层

定义所有API的请求响应模型、错误码和分页规范
"""

from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, field_validator


# 泛型类型
T = TypeVar("T")


class ErrorCode(str, Enum):
    """统一错误码"""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    PLUGIN_ERROR = "PLUGIN_ERROR"
    EXTERNAL_SERVICE_ERROR = "EXTERNAL_SERVICE_ERROR"


class ErrorDetail(BaseModel):
    """错误详情"""

    code: ErrorCode = Field(..., description="错误代码")
    message: str = Field(..., description="错误消息")
    details: dict[str, Any] | None = Field(default=None, description="详细错误信息")
    trace_id: str | None = Field(default=None, description="追踪ID")


class ApiResponse(BaseModel, Generic[T]):
    """统一API响应格式"""

    success: bool = Field(..., description="是否成功")
    data: T | None = Field(default=None, description="响应数据")
    error: ErrorDetail | None = Field(default=None, description="错误信息")
    timestamp: str = Field(..., description="时间戳")
    trace_id: str | None = Field(default=None, description="追踪ID")

    @field_validator("timestamp", mode="before")
    @classmethod
    def convert_timestamp(cls, v):
        """自动将 datetime 转换为 ISO 格式字符串"""
        if isinstance(v, datetime):
            return v.isoformat()
        return v


class PaginationParams(BaseModel):
    """分页参数"""

    page: int = Field(default=1, ge=1, description="页码")
    size: int = Field(default=20, ge=1, le=100, description="每页大小")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class PaginatedResponse(BaseModel, Generic[T]):
    """分页响应"""

    items: list[T] = Field(..., description="数据列表")
    total: int = Field(..., description="总数")
    page: int = Field(..., description="当前页码")
    size: int = Field(..., description="每页大小")
    pages: int = Field(..., description="总页数")

    @classmethod
    def create(cls, items: list[T], total: int, page: int, size: int):
        pages = (total + size - 1) // size if total > 0 else 0
        return cls(items=items, total=total, page=page, size=size, pages=pages)


class HealthStatus(BaseModel):
    """健康检查状态"""

    service: str = Field(..., description="服务名称")
    status: str = Field(..., description="状态: healthy, unhealthy, degraded")
    timestamp: str = Field(..., description="检查时间")
    details: dict[str, Any] | None = Field(default=None, description="详细信息")

    @field_validator("timestamp", mode="before")
    @classmethod
    def convert_timestamp(cls, v):
        """自动将 datetime 转换为 ISO 格式字符串"""
        if isinstance(v, datetime):
            return v.isoformat()
        return v


class HealthResponse(BaseModel):
    """健康检查响应"""

    status: str = Field(..., description="整体状态")
    version: str = Field(..., description="版本")
    environment: str = Field(..., description="环境")
    checks: list[HealthStatus] = Field(..., description="检查项目")
    uptime: float | None = Field(default=None, description="运行时间(秒)")


class StandardResponse(BaseModel, Generic[T]):
    """标准响应格式（兼容前端）"""

    code: int = Field(default=200, description="状态码")
    msg: str = Field(default="success", description="消息")
    data: T | None = Field(default=None, description="响应数据")

    @classmethod
    def success(cls, data: T | None = None, msg: str = "success"):
        """成功响应"""
        return cls(code=200, msg=msg, data=data)

    @classmethod
    def error(cls, msg: str, code: int = 500):
        """错误响应"""
        return cls(code=code, msg=msg, data=None)
