"""
服务注册表

管理所有服务的生命周期和依赖关系
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from infra.logging import get_logger


logger = get_logger(__name__)


@dataclass
class ServiceConfig:
    """服务配置"""

    name: str
    implementation: type
    singleton: bool = True
    factory: Callable | None = None
    dependencies: list | None = None
    init_priority: int = 100  # 数字越小优先级越高


class ServiceLifecycle(ABC):
    """服务生命周期接口"""

    @abstractmethod
    async def start(self) -> None:
        """启动服务"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """停止服务"""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查"""
        pass


class ServiceRegistry:
    """服务注册表"""

    def __init__(self):
        self._services: dict[str, ServiceConfig] = {}
        self._instances: dict[str, Any] = {}
        self._started: bool = False

    def register(self, config: ServiceConfig):
        """注册服务"""
        self._services[config.name] = config
        logger.info(f"注册服务: {config.name}")

    def register_instance(self, name: str, instance: Any):
        """注册服务实例"""
        self._instances[name] = instance
        logger.info(f"注册服务实例: {name}")

    def get(self, name: str) -> Any:
        """获取服务实例"""
        # 检查是否已有实例
        if name in self._instances:
            return self._instances[name]

        # 检查是否有配置
        if name not in self._services:
            raise ValueError(f"服务未注册: {name}")

        config = self._services[name]

        # 创建实例
        if config.factory:
            instance = config.factory()
        else:
            # 简单的依赖注入
            if config.dependencies:
                deps = {dep: self.get(dep) for dep in config.dependencies}
                instance = config.implementation(**deps)
            else:
                instance = config.implementation()

        # 如果是单例，缓存实例
        if config.singleton:
            self._instances[name] = instance

        return instance

    async def start_all(self):
        """启动所有服务"""
        if self._started:
            return

        logger.info("开始启动所有服务...")

        # 按优先级排序
        sorted_services = sorted(self._services.values(), key=lambda x: x.init_priority)

        for config in sorted_services:
            try:
                instance = self.get(config.name)
                if isinstance(instance, ServiceLifecycle):
                    await instance.start()
                    logger.info(f"服务启动成功: {config.name}")
            except Exception as e:
                logger.error(f"服务启动失败: {config.name} - {e}")
                raise

        self._started = True
        logger.info("所有服务启动完成")

    async def stop_all(self):
        """停止所有服务"""
        if not self._started:
            return

        logger.info("开始停止所有服务...")

        # 按相反顺序停止
        sorted_services = sorted(
            self._services.values(), key=lambda x: x.init_priority, reverse=True
        )

        for config in sorted_services:
            if config.name in self._instances:
                try:
                    instance = self._instances[config.name]
                    if isinstance(instance, ServiceLifecycle):
                        await instance.stop()
                        logger.info(f"服务停止成功: {config.name}")
                except Exception as e:
                    logger.error(f"服务停止失败: {config.name} - {e}")

        self._started = False
        logger.info("所有服务停止完成")

    async def health_check_all(self) -> dict[str, bool]:
        """检查所有服务健康状态"""
        results = {}

        for name, instance in self._instances.items():
            if isinstance(instance, ServiceLifecycle):
                try:
                    results[name] = await instance.health_check()
                except Exception as e:
                    logger.error(f"服务健康检查失败: {name} - {e}")
                    results[name] = False
            else:
                results[name] = True  # 如果没有实现健康检查，假设健康

        return results

    def list_services(self) -> dict[str, dict[str, Any]]:
        """列出所有注册的服务"""
        return {
            name: {
                "implementation": config.implementation.__name__,
                "singleton": config.singleton,
                "init_priority": config.init_priority,
                "has_instance": name in self._instances,
                "dependencies": config.dependencies or [],
            }
            for name, config in self._services.items()
        }


# 全局服务注册表
service_registry = ServiceRegistry()
