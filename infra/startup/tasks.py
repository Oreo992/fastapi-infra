"""图像任务服务初始化"""

from app.core.logging_pkg.logger import get_logger
from app.services.platform.image.image_task_service import image_task_service

logger = get_logger(__name__)


async def init_image_tasks():
    """初始化图像任务服务"""
    try:
        await image_task_service.initialize()
        logger.info("图像任务服务初始化完成")

        try:
            stats = await image_task_service.get_queue_stats()
            if "error" in stats:
                logger.warning(f"获取队列统计失败: {stats['error']}")
            elif "queue" in stats:
                pending_count = stats["queue"]["pending"]
                if pending_count > 0:
                    logger.info(f"发现 {pending_count} 个待处理任务，触发处理...")
                    await image_task_service.processor.notify_new_task()
                    logger.info("积压任务处理已触发")
                else:
                    logger.info("没有待处理的积压任务")
        except Exception as recovery_error:
            logger.error(f"积压任务检查失败: {recovery_error}")

    except Exception as e:
        logger.error(f"图像任务服务初始化失败: {e}")
        logger.warning("图像任务服务启动失败，图像生成功能将不可用")
