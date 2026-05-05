"""
高级示例 - 完整功能展示

展示 fastapi-infra 的所有高级功能：
- 生命周期管理
- 中间件集成
- 并发控制
- 流式响应
- API契约
"""

from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager

from infra import (
    # 核心
    get_logger,
    DatabaseManager,
    CacheService,
    BaseSettings,
    # 中间件
    RequestLoggingMiddleware,
    ErrorStrategy,
    # 生命周期
    LifecycleManager,
    # 并发
    GlobalThreadPoolManager,
    # 流式
    StreamsManager,
    # API契约
    ApiResponse,
    ErrorCode,
    ErrorDetail,
)
from infra.utils import get_timestamp

# 配置和日志
settings = BaseSettings()
logger = get_logger(__name__)

# 基础设施组件
db_config = {
    "mysql_host": settings.mysql_host,
    "mysql_port": settings.mysql_port,
    "mysql_user": settings.mysql_user,
    "mysql_password": settings.mysql_password,
    "mysql_db": settings.mysql_db,
    "redis_url": settings.redis_url,
    "debug": settings.debug,
}

db_manager = DatabaseManager(db_config)
cache = CacheService(namespace="advanced_app")
streams_manager = StreamsManager(db_manager, stream_name="app_tasks")

# 创建应用
app = FastAPI(
    title="FastAPI Infra Advanced",
    version="1.0.0",
    debug=settings.debug,
)

# 添加中间件
app.add_middleware(RequestLoggingMiddleware)

# 生命周期管理
lifecycle = LifecycleManager(app)


# ==================== 启动回调 ====================
async def init_database():
    """初始化数据库连接"""
    logger.info("初始化数据库连接池...")
    await db_manager.initialize()
    logger.info("数据库连接池已就绪")


async def init_thread_pools():
    """初始化线程池"""
    logger.info("初始化全局线程池...")
    pool_config = {
        "compute_thread_pool_size": 4,
        "io_thread_pool_size": 10,
    }
    await GlobalThreadPoolManager.get_instance(pool_config)
    logger.info("全局线程池已就绪")


async def warmup_cache():
    """预热缓存"""
    logger.info("预热应用缓存...")
    await cache.set("app_status", "ready", ttl=3600)
    logger.info("缓存预热完成")


async def init_streams():
    """初始化流式管理器"""
    logger.info("初始化 Redis Streams...")
    await streams_manager.initialize()
    logger.info("Redis Streams 已就绪")


# 注册启动回调
lifecycle.add_startup_callback(init_database, "database")
lifecycle.add_startup_callback(init_thread_pools, "thread_pools")
lifecycle.add_startup_callback(warmup_cache, "cache")
lifecycle.add_startup_callback(init_streams, "streams")


# ==================== 关闭回调 ====================
async def cleanup_streams():
    """清理流式管理器"""
    logger.info("停止 Redis Streams 消费者...")
    await streams_manager.close()


async def cleanup_database():
    """关闭数据库连接"""
    logger.info("关闭数据库连接池...")
    await db_manager.close()


lifecycle.add_shutdown_callback(cleanup_streams, "streams")
lifecycle.add_shutdown_callback(cleanup_database, "database")

# 注册生命周期
lifecycle.register()


# ==================== API 路由 ====================

@app.get("/")
async def root():
    """首页"""
    app_status = await cache.get("app_status")
    return ApiResponse(
        success=True,
        data={
            "message": "FastAPI Infra Advanced Example",
            "version": "1.0.0",
            "status": app_status or "unknown",
        },
        timestamp=str(get_timestamp()),
    )


@app.get("/health")
async def health_check():
    """健康检查"""
    try:
        # 检查数据库
        db_healthy = await db_manager.health_check()
        
        # 检查缓存
        cache_test = await cache.get("app_status")
        cache_healthy = cache_test is not None
        
        return ApiResponse(
            success=True,
            data={
                "status": "healthy" if (db_healthy and cache_healthy) else "degraded",
                "components": {
                    "database": "healthy" if db_healthy else "unhealthy",
                    "cache": "healthy" if cache_healthy else "unhealthy",
                },
            },
            timestamp=str(get_timestamp()),
        )
    except Exception as e:
        logger.error("健康检查失败", exc_info=True)
        return ApiResponse(
            success=False,
            error=ErrorDetail(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"Health check failed: {str(e)}",
            ),
            timestamp=str(get_timestamp()),
        )


@app.post("/tasks")
async def create_task(task_type: str, params: dict):
    """创建异步任务（通过 Redis Streams）"""
    try:
        task_id = f"task_{get_timestamp()}"
        message_id = await streams_manager.add_message(
            task_id=task_id,
            task_type=task_type,
            params=params,
            priority=5,
        )
        
        return ApiResponse(
            success=True,
            data={
                "task_id": task_id,
                "message_id": message_id,
                "status": "pending",
            },
            timestamp=str(get_timestamp()),
        )
    except Exception as e:
        logger.error("创建任务失败", exc_info=True)
        return ApiResponse(
            success=False,
            error=ErrorDetail(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"Failed to create task: {str(e)}",
            ),
            timestamp=str(get_timestamp()),
        )


@app.get("/cache/demo")
async def cache_demo():
    """缓存演示"""
    # 测试缓存
    key = "demo_key"
    cached_value = await cache.get(key)
    
    if cached_value:
        return ApiResponse(
            success=True,
            data={"source": "cache", "value": cached_value},
            timestamp=str(get_timestamp()),
        )
    
    # 模拟数据库查询
    new_value = {"data": "from database", "timestamp": get_timestamp()}
    await cache.set(key, new_value, ttl=60)
    
    return ApiResponse(
        success=True,
        data={"source": "database", "value": new_value},
        timestamp=str(get_timestamp()),
    )


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="info" if not settings.debug else "debug",
    )
