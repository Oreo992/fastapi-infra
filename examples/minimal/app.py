"""
最小化示例 - FastAPI + 基础设施包

演示如何使用 fastapi-infra 包
"""

from fastapi import FastAPI
from infra.database import DatabaseManager
from infra.cache import CacheService
from infra.logging import get_logger

# 创建日志
logger = get_logger(__name__)

# 创建 FastAPI 应用
app = FastAPI(title="FastAPI Infra 最小化示例")

# 创建基础设施（使用默认配置）
db_manager = DatabaseManager()
cache = CacheService(namespace="example")


@app.on_event("startup")
async def startup():
    """应用启动时初始化基础设施"""
    await db_manager.initialize()
    logger.info("应用启动完成")


@app.on_event("shutdown")
async def shutdown():
    """应用关闭时清理资源"""
    await db_manager.close()
    logger.info("应用关闭完成")


@app.get("/")
async def root():
    """根路径"""
    return {"message": "Hello from FastAPI Infra!", "status": "ok"}


@app.get("/health")
async def health():
    """健康检查"""
    db_healthy = await db_manager.health_check()
    return {
        "status": "healthy" if db_healthy else "unhealthy",
        "database": db_healthy,
    }


@app.get("/cache/test")
async def cache_test():
    """测试缓存功能"""
    # 设置缓存
    await cache.set("test_key", {"data": "test_value", "count": 42}, ttl=60)
    
    # 读取缓存
    value = await cache.get("test_key")
    
    return {"cached_value": value}
