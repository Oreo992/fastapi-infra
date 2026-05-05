"""统一异常模块"""

from infra.exceptions.base import (
    # 基础异常
    AppException,
    # 领域/业务异常
    DomainException,
    EntityNotFoundError,
    ValidationError,
    BusinessRuleViolationError,
    # 基础设施异常
    InfrastructureException,
    RepositoryError,
    DatabaseError,
    CacheError,
    # 外部集成异常
    IntegrationException,
    ExternalServiceError,
    LLMServiceError,
    AIServiceError,
)

__all__ = [
    # 基础异常
    "AppException",
    # 领域/业务异常
    "DomainException",
    "EntityNotFoundError",
    "ValidationError",
    "BusinessRuleViolationError",
    # 基础设施异常
    "InfrastructureException",
    "RepositoryError",
    "DatabaseError",
    "CacheError",
    # 外部集成异常
    "IntegrationException",
    "ExternalServiceError",
    "LLMServiceError",
    "AIServiceError",
]
