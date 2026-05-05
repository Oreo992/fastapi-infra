"""通用模块

API 契约和响应规范：
- ApiResponse: 统一API响应格式
- ErrorCode: 统一错误码
- ErrorDetail: 错误详情
- PaginatedResponse: 分页响应
"""

from infra.common.contracts import (
    ApiResponse,
    ErrorCode,
    ErrorDetail,
    PaginatedResponse,
)

__all__ = [
    "ApiResponse",
    "ErrorCode",
    "ErrorDetail",
    "PaginatedResponse",
]
