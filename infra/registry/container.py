"""
服务工厂和依赖注入容器

实现依赖注入模式，统一管理所有服务和DAO的创建和生命周期。

架构说明：
- ServiceContainer: 服务容器，实现依赖注入
- ServiceFactory: 服务工厂，负责配置和创建服务容器
- 服务模块: 按领域拆分的服务注册模块（见 service_modules/）

使用方式：
    # 获取服务容器
    container = ServiceFactory.get_container()

    # 通过接口获取服务
    service = container.get(IService)

    # 通过名称获取服务
    service = service_registry.get("resource_service")
"""

import inspect
from typing import Any, TypeVar

from infra.registry import service_registry
from infra.logging import get_logger


logger = get_logger(__name__)

T = TypeVar("T")


class ServiceContainer:
    """
    服务容器 - 实现依赖注入

    支持三种注册模式：
    - singleton: 单例服务，整个应用生命周期只创建一次
    - transient: 瞬时服务，每次获取都创建新实例
    - factory: 工厂方法，通过自定义逻辑创建实例
    """

    def __init__(self):
        self._services: dict[type, Any] = {}  # 瞬时服务
        self._singletons: dict[type, Any] = {}  # 单例服务类型
        self._singleton_instances: dict[str, Any] = {}  # 单例实例缓存
        self._factories: dict[type, callable] = {}  # 工厂方法

    def register_singleton(self, interface: type[T], implementation: type[T]) -> None:
        """注册单例服务"""
        self._singletons[interface] = implementation

    def register_transient(self, interface: type[T], implementation: type[T]) -> None:
        """注册瞬时服务（每次调用创建新实例）"""
        self._services[interface] = implementation

    def register_factory(self, interface: type[T], factory: callable) -> None:
        """注册工厂方法"""
        self._factories[interface] = factory

    def get(self, interface: type[T]) -> T:
        """获取服务实例"""
        # 优先检查工厂方法
        if interface in self._factories:
            return self._factories[interface]()

        # 检查单例
        if interface in self._singletons:
            cache_key = f"_singleton_{interface.__name__}"
            if cache_key not in self._singleton_instances:
                implementation = self._singletons[interface]
                instance = self._create_instance(implementation)
                self._singleton_instances[cache_key] = instance
            return self._singleton_instances[cache_key]

        # 检查瞬时服务
        if interface in self._services:
            implementation = self._services[interface]
            return self._create_instance(implementation)

        raise ValueError(f"服务 {interface.__name__} 未注册")

    def has(self, interface: type[T]) -> bool:
        """检查服务是否已注册"""
        return (
            interface in self._factories
            or interface in self._singletons
            or interface in self._services
        )

    def _create_instance(self, implementation: type[T]) -> T:
        """创建服务实例（支持构造函数依赖注入）"""
        try:
            sig = inspect.signature(implementation.__init__)
            params = {}

            for param_name, param in sig.parameters.items():
                if param_name == "self":
                    continue

                if param.annotation != inspect.Parameter.empty:
                    try:
                        params[param_name] = self.get(param.annotation)
                    except ValueError:
                        # 依赖无法解析，使用默认值
                        if param.default != inspect.Parameter.empty:
                            params[param_name] = param.default
                        else:
                            params[param_name] = None

            return implementation(**params)
        except (TypeError, ValueError) as e:
            logger.debug(f"依赖注入失败，尝试无参构造: {e}")
            return implementation()

    def clear(self) -> None:
        """清空所有注册（用于测试）"""
        self._services.clear()
        self._singletons.clear()
        self._singleton_instances.clear()
        self._factories.clear()


class ServiceFactory:
    """
    服务工厂 - 负责配置和创建服务容器

    使用模块化注册，服务注册逻辑位于 service_modules/ 目录下
    """

    _container: ServiceContainer | None = None
    _initialized: bool = False

    @classmethod
    def get_container(cls) -> ServiceContainer:
        """获取服务容器（单例）"""
        if cls._container is None:
            cls._container = ServiceContainer()
            cls._configure_services()
            cls._initialized = True
        return cls._container

    @classmethod
    def _configure_services(cls, register_func=None):
        """配置所有服务的依赖关系
        
        Args:
            register_func: 可选的服务注册函数，接收 (registry, container) 参数
                          如果不提供，则跳过自动注册，由使用者手动注册服务
        """
        if register_func:
            logger.info("开始模块化服务注册...")
            register_func(service_registry, cls._container)
            logger.info("模块化服务注册完成")
        else:
            logger.debug("未提供服务注册函数，跳过自动注册")

    @classmethod
    def get_service(cls, interface: type[T]) -> T:
        """获取服务实例"""
        container = cls.get_container()
        return container.get(interface)

    @classmethod
    def reset(cls):
        """重置容器（用于测试）"""
        if cls._container:
            cls._container.clear()
        cls._container = None
        cls._initialized = False

    @classmethod
    def is_initialized(cls) -> bool:
        """检查是否已初始化"""
        return cls._initialized
