"""Shared API contracts for application routes and infrastructure errors."""

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Generic, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ErrorCode(str, Enum):
    """Stable error codes used by middleware and application routes."""

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
    """Structured error payload."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode = Field(..., description="错误代码")
    message: str = Field(..., description="错误消息")
    details: dict[str, Any] | None = Field(default=None, description="详细错误信息")
    trace_id: str | None = Field(default=None, description="追踪ID")


class ApiResponse(BaseModel, Generic[T]):
    """Single response envelope used by infrastructure helpers."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="是否成功")
    data: T | None = Field(default=None, description="响应数据")
    error: ErrorDetail | None = Field(default=None, description="错误信息")
    timestamp: str = Field(default_factory=_now_iso, description="时间戳")
    trace_id: str | None = Field(default=None, description="追踪ID")

    @classmethod
    def ok(cls, data: T | None = None, *, trace_id: str | None = None) -> Self:
        return cls(success=True, data=data, trace_id=trace_id)

    @classmethod
    def fail(
        cls,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> Self:
        return cls(
            success=False,
            error=ErrorDetail(
                code=code,
                message=message,
                details=details,
                trace_id=trace_id,
            ),
            trace_id=trace_id,
        )

    @field_validator("timestamp", mode="before")
    @classmethod
    def convert_timestamp(cls, value: object) -> object:
        if isinstance(value, datetime):
            return value.isoformat()
        return value


class PaginationParams(BaseModel):
    """Validated page-number pagination input."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="页码")
    size: int = Field(default=20, ge=1, le=100, description="每页大小")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class PaginatedResponse(BaseModel, Generic[T]):
    """Page-number pagination output."""

    model_config = ConfigDict(extra="forbid")

    items: list[T] = Field(..., description="数据列表")
    total: int = Field(..., ge=0, description="总数")
    page: int = Field(..., ge=1, description="当前页码")
    size: int = Field(..., ge=1, description="每页大小")
    pages: int = Field(..., ge=0, description="总页数")

    @classmethod
    def create(cls, items: list[T], total: int, page: int, size: int) -> Self:
        pages = (total + size - 1) // size if total > 0 else 0
        return cls(items=items, total=total, page=page, size=size, pages=pages)
