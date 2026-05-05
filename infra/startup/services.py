"""线程池 + 服务工厂 + 注册表初始化"""

from app.core.common.registry import service_registry
from app.core.concurrency.thread_pool import GlobalThreadPoolManager
from app.core.logging_pkg.logger import get_logger

logger = get_logger(__name__)


async def init_services():
    """初始化全局线程池、服务工厂和注册表"""
    try:
        await GlobalThreadPoolManager.get_instance()
        logger.info("全局线程池初始化完成")
    except Exception as e:
        logger.warning(f"全局线程池初始化失败: {e}")

    try:
        from app.core.common.service_factory import ServiceFactory

        ServiceFactory.get_container()
        logger.info("服务工厂初始化完成")
    except Exception as e:
        logger.warning(f"服务工厂初始化失败: {e}")

    try:
        await service_registry.start_all()
        logger.info("服务注册表服务启动完成")
    except Exception as e:
        logger.warning(f"服务注册表服务启动失败: {e}")
