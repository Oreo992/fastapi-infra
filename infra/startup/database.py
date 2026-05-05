"""数据库连接初始化"""

from app.core.infrastructure.db.database import init_database
from app.core.logging_pkg.logger import get_logger

logger = get_logger(__name__)


async def init_database_connections():
    """初始化数据库连接（Redis和MySQL）"""
    await init_database()
    logger.info("数据库连接初始化完成")
