"""
统一异常体系

定义系统中使用的各种异常类型，遵循以下层次结构：

BaseException (Python内置)
└── Exception (Python内置)
    └── AppException (应用基础异常)
        ├── DomainException (领域/业务异常)
        │   ├── EntityNotFoundError
        │   ├── ValidationError
        │   └── BusinessRuleViolationError
        ├── InfrastructureException (基础设施异常)
        │   ├── RepositoryError
        │   ├── DatabaseError
        │   └── CacheError
        ├── IntegrationException (外部集成异常)
        │   ├── ExternalServiceError
        │   ├── LLMServiceError
        │   └── AIServiceError
        └── PersonalityTestException (遗留兼容)
            └── ...

使用指南：
- API 层：捕获 AppException，转换为 HTTP 响应
- Service 层：抛出 DomainException
- Repository 层：抛出 RepositoryError
- Client 层：抛出 IntegrationException
"""

from typing import Any


# ==================== 基础异常 ====================


class AppException(Exception):
    """
    应用基础异常

    所有自定义异常的基类，提供统一的错误信息结构
    """

    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ):
        self.message = message
        self.error_code = error_code or self._default_error_code()
        self.details = details or {}
        self.cause = cause
        super().__init__(self.message)

    def _default_error_code(self) -> str:
        """默认错误码（子类可重写）"""
        return "INTERNAL_ERROR"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式（用于 API 响应）"""
        result: dict[str, Any] = {
            "code": self.error_code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


# ==================== 领域/业务异常 ====================


class DomainException(AppException):
    """
    领域异常基类

    用于业务规则违反、实体不存在等业务层面的错误
    """

    def _default_error_code(self) -> str:
        return "DOMAIN_ERROR"


class EntityNotFoundError(DomainException):
    """
    实体未找到异常

    当请求的实体（Agent、Tool、Knowledge 等）不存在时抛出
    """

    def __init__(
        self,
        entity_type: str,
        entity_id: str,
        message: str | None = None,
    ):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(
            message=message or f"{entity_type} with id '{entity_id}' not found",
            error_code="ENTITY_NOT_FOUND",
            details={"entity_type": entity_type, "entity_id": entity_id},
        )


class BusinessRuleViolationError(DomainException):
    """
    业务规则违反异常

    当操作违反业务规则时抛出（如：删除正在使用的资源）
    """

    def __init__(
        self,
        rule: str,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        self.rule = rule
        super().__init__(
            message=message,
            error_code="BUSINESS_RULE_VIOLATION",
            details={"rule": rule, **(details or {})},
        )


class DuplicateEntityError(DomainException):
    """
    实体重复异常

    当创建的实体已存在时抛出
    """

    def __init__(
        self,
        entity_type: str,
        field: str,
        value: Any,
    ):
        super().__init__(
            message=f"{entity_type} with {field}='{value}' already exists",
            error_code="DUPLICATE_ENTITY",
            details={"entity_type": entity_type, "field": field, "value": value},
        )


# ==================== 基础设施异常 ====================


class InfrastructureException(AppException):
    """
    基础设施异常基类

    用于数据库、缓存、消息队列等基础设施层面的错误
    """

    def _default_error_code(self) -> str:
        return "INFRASTRUCTURE_ERROR"


class RepositoryError(InfrastructureException):
    """
    Repository 操作异常

    数据访问层操作失败时抛出
    """

    def __init__(
        self,
        operation: str,
        entity_type: str,
        message: str,
        cause: Exception | None = None,
    ):
        self.operation = operation
        self.entity_type = entity_type
        super().__init__(
            message=f"Repository {operation} failed for {entity_type}: {message}",
            error_code="REPOSITORY_ERROR",
            details={
                "operation": operation,
                "entity_type": entity_type,
                "original_message": message,
            },
            cause=cause,
        )


class DatabaseError(InfrastructureException):
    """数据库操作异常"""

    def __init__(self, operation: str, message: str):
        super().__init__(
            message=f"数据库操作 {operation} 失败: {message}",
            error_code="DATABASE_ERROR",
            details={"operation": operation, "original_message": message},
        )


class CacheError(InfrastructureException):
    """缓存操作异常"""

    def __init__(self, operation: str, message: str):
        super().__init__(
            message=f"缓存操作 {operation} 失败: {message}",
            error_code="CACHE_ERROR",
            details={"operation": operation, "original_message": message},
        )


# ==================== 外部集成异常 ====================


class IntegrationException(AppException):
    """
    外部集成异常基类

    用于外部 API、LLM 服务等第三方集成的错误
    """

    def _default_error_code(self) -> str:
        return "INTEGRATION_ERROR"


class ExternalServiceError(IntegrationException):
    """外部服务异常"""

    def __init__(self, service_name: str, message: str):
        self.service_name = service_name
        super().__init__(
            message=f"外部服务 {service_name} 错误: {message}",
            error_code="EXTERNAL_API_ERROR",
            details={"service_name": service_name, "original_message": message},
        )


class LLMServiceError(IntegrationException):
    """LLM服务异常"""

    def __init__(self, service_name: str, message: str):
        super().__init__(
            message=f"{service_name} 服务错误: {message}",
            error_code="LLM_SERVICE_ERROR",
            details={"service_name": service_name, "original_message": message},
        )


class AIServiceError(IntegrationException):
    """AI服务异常"""

    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(
            message=message,
            error_code=error_code or "AI_SERVICE_ERROR",
            details={"original_message": message},
        )


# ==================== 遗留兼容异常（保持向后兼容） ====================


class PersonalityTestException(AppException):
    """
    性格测试系统基础异常

    [遗留] 保持向后兼容，新代码请使用 AppException 或其子类
    """

    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
        )


class SessionNotFoundError(EntityNotFoundError):
    """
    会话未找到异常

    [遗留兼容] 继承自 EntityNotFoundError
    """

    def __init__(self, session_id: str):
        super().__init__(
            entity_type="Session",
            entity_id=session_id,
            message=f"会话 {session_id} 未找到",
        )


class SessionExpiredError(DomainException):
    """会话过期异常"""

    def __init__(self, session_id: str):
        super().__init__(
            message=f"会话 {session_id} 已过期",
            error_code="SESSION_EXPIRED",
            details={"session_id": session_id},
        )


class InvalidQuestionError(DomainException):
    """无效问题异常"""

    def __init__(self, question_id: str):
        super().__init__(
            message=f"问题 {question_id} 无效或不存在",
            error_code="INVALID_QUESTION",
            details={"question_id": question_id},
        )


class WorkflowExecutionError(DomainException):
    """工作流执行异常"""

    def __init__(self, workflow_name: str, step: str, message: str):
        super().__init__(
            message=f"工作流 {workflow_name} 在步骤 {step} 执行失败: {message}",
            error_code="WORKFLOW_EXECUTION_ERROR",
            details={
                "workflow_name": workflow_name,
                "step": step,
                "original_message": message,
            },
        )


class ValidationError(DomainException):
    """
    数据验证异常

    支持单字段和多字段验证错误
    """

    def __init__(
        self,
        message: str,
        field: str | None = None,
        value: Any = None,
        errors: list[dict[str, Any]] | None = None,
    ):
        details: dict[str, Any]
        if errors:
            # 多字段验证错误
            details = {"errors": errors}
            formatted_message = message
        elif field is not None:
            # 单字段验证错误
            formatted_message = f"字段 {field} 验证失败: {message}"
            details = {"field": field, "value": value, "validation_message": message}
        else:
            formatted_message = message
            details = {"validation_message": message}

        super().__init__(
            message=formatted_message,
            error_code="VALIDATION_ERROR",
            details=details,
        )


class ConfigurationError(InfrastructureException):
    """配置错误异常"""

    def __init__(self, config_name: str, message: str):
        super().__init__(
            message=f"配置 {config_name} 错误: {message}",
            error_code="CONFIGURATION_ERROR",
            details={"config_name": config_name, "original_message": message},
        )


# ==================== 便捷导出（保持向后兼容） ====================

__all__ = [
    # 基础异常
    "AppException",
    # 领域异常
    "DomainException",
    "EntityNotFoundError",
    "BusinessRuleViolationError",
    "DuplicateEntityError",
    "ValidationError",
    # 基础设施异常
    "InfrastructureException",
    "RepositoryError",
    "DatabaseError",
    "CacheError",
    "ConfigurationError",
    # 外部集成异常
    "IntegrationException",
    "ExternalServiceError",
    "LLMServiceError",
    "AIServiceError",
    # 遗留兼容
    "PersonalityTestException",
    "SessionNotFoundError",
    "SessionExpiredError",
    "InvalidQuestionError",
    "WorkflowExecutionError",
]
