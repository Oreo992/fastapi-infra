"""队列系统初始化"""

from app.core.configuration.config import settings
from app.core.logging_pkg.logger import get_logger

logger = get_logger(__name__)


async def init_queues():
    """初始化统一队列系统"""
    try:
        from app.services.queue import queue_manager

        _ = queue_manager.get_or_create_queue(
            queue_name="app_tasks",
            consumer_group="app_workers",
            consumer_name="worker_default",
        )
        _ = queue_manager.get_or_create_queue(
            queue_name="image_tasks",
            consumer_group="image_workers",
            consumer_name="image_worker_1",
        )
        await queue_manager.initialize_all()
        logger.info(f"统一队列系统初始化完成，队列类型: {settings.queue_type}")
    except Exception as e:
        logger.warning(f"统一队列系统初始化失败: {e}")
