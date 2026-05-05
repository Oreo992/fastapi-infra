"""服务注册与依赖注入模块"""

from infra.registry.registry import ServiceRegistry, ServiceConfig, ServiceLifecycle, service_registry
from infra.registry.container import ServiceContainer, ServiceFactory

__all__ = [
    "ServiceRegistry",
    "ServiceConfig",
    "ServiceLifecycle",
    "service_registry",
    "ServiceContainer",
    "ServiceFactory",
]
