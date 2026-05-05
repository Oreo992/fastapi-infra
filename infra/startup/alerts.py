"""告警服务初始化"""

from app.core.logging_pkg.logger import get_logger

logger = get_logger(__name__)


async def init_alerts():
    """启动告警服务workers"""
    try:
        from app.services.platform.alert.alert_service import get_alert_service

        alert_service = get_alert_service()
        alert_service.start_workers()
        logger.info("告警服务workers已启动")
    except Exception as e:
        logger.warning(f"告警服务启动失败: {e}")
