"""性能监控和Langfuse初始化"""

from app.core.logging_pkg.logger import get_logger
from app.core.observability.performance_monitor import performance_monitor

logger = get_logger(__name__)


async def init_monitoring():
    """启动性能监控和Langfuse"""
    await performance_monitor.start_monitoring(interval=60)
    logger.info("性能监控已启动")

    try:
        from app.core.observability.langfuse_client import get_langfuse_manager

        langfuse_manager = get_langfuse_manager()
        if langfuse_manager.is_enabled:
            logger.info("Langfuse 追踪已启用")
        else:
            logger.info("Langfuse 追踪未启用")
    except (ImportError, TypeError):
        logger.info("Langfuse 不可用，跳过初始化")
    except Exception as e:
        logger.warning(f"Langfuse 初始化失败: {e}")
