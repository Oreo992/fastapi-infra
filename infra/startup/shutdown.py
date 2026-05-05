"""统一关闭逻辑"""

import asyncio

from fastapi import FastAPI

from app.core.common.registry import service_registry
from app.core.concurrency.thread_pool import GlobalThreadPoolManager
from app.core.infrastructure.http.http_client import HttpClientManager
from app.core.logging_pkg.logger import get_logger
from app.core.observability.performance_monitor import performance_monitor
from app.services.platform.image.image_task_service import image_task_service
from app.services.llm import cleanup_llm_service

logger = get_logger(__name__)


async def graceful_shutdown(app: FastAPI):
    """执行优雅关闭，总超时10秒"""
    logger.info("收到关闭信号,开始优雅关闭...")
    shutdown_timeout = 10.0

    try:
        async with asyncio.timeout(shutdown_timeout):
            # 1. 停止MCP工具缓存调度器
            logger.info("停止MCP工具缓存调度器...")
            try:
                from app.services.platform.mcp.mcp_cache_scheduler import stop_mcp_cache_scheduler
                await asyncio.wait_for(stop_mcp_cache_scheduler(), timeout=1.0)
                logger.info("MCP工具缓存调度器已停止")
            except TimeoutError:
                logger.warning("MCP缓存调度器关闭超时")
            except Exception as e:
                logger.warning(f"MCP缓存调度器停止失败: {e}")

            # 2. 关闭记忆服务
            logger.info("关闭记忆服务...")
            try:
                memory_service = service_registry.get("memory_service")
                if hasattr(memory_service, "close"):
                    await asyncio.wait_for(memory_service.close(), timeout=2.0)
                    logger.info("记忆服务已关闭")
            except TimeoutError:
                logger.warning("记忆服务关闭超时")
            except Exception as e:
                logger.warning(f"记忆服务关闭失败: {e}")

            # 3. 停止告警服务workers
            logger.info("停止告警服务workers...")
            try:
                from app.services.platform.alert.alert_service import get_alert_service
                alert_service = get_alert_service()
                await asyncio.wait_for(alert_service.stop_workers(), timeout=2.0)
                logger.info("告警服务workers已停止")
            except TimeoutError:
                logger.warning("告警服务关闭超时")
            except Exception as e:
                logger.warning(f"告警服务停止失败: {e}")

            # 4. 停止图像任务服务
            logger.info("停止图像任务服务...")
            try:
                await asyncio.wait_for(image_task_service.shutdown(), timeout=2.0)
                logger.info("图像任务服务已停止")
            except TimeoutError:
                logger.warning("图像任务服务关闭超时,强制继续")
            except Exception as e:
                logger.warning(f"图像任务服务停止失败: {e}")

            # 5. 关闭Langfuse客户端
            try:
                from app.core.observability.langfuse_client import get_langfuse_manager
                logger.info("关闭Langfuse客户端...")
                langfuse_manager = get_langfuse_manager()
                langfuse_manager.shutdown()
                logger.info("Langfuse客户端已关闭")
            except (ImportError, TypeError):
                pass
            except Exception as e:
                logger.warning(f"Langfuse客户端关闭失败: {e}")

            # 6. 关闭线程池
            logger.info("关闭全局线程池...")
            try:
                thread_pool_manager = await GlobalThreadPoolManager.get_instance()
                await thread_pool_manager.shutdown(wait=True, timeout=3.0)
                logger.info("全局线程池已关闭")
            except Exception as e:
                logger.warning(f"全局线程池关闭失败: {e}")

            # 7. 停止性能监控
            logger.info("停止性能监控...")
            try:
                await asyncio.wait_for(performance_monitor.stop_monitoring(), timeout=1.0)
                logger.info("性能监控已停止")
            except TimeoutError:
                logger.warning("性能监控关闭超时")
            except Exception as e:
                logger.warning(f"性能监控停止失败: {e}")

            # 8. 停止服务注册表
            logger.info("停止服务注册表...")
            try:
                await asyncio.wait_for(service_registry.stop_all(), timeout=2.0)
                logger.info("服务注册表服务停止完成")
            except TimeoutError:
                logger.warning("服务注册表关闭超时")
            except Exception as e:
                logger.warning(f"服务注册表停止失败: {e}")

            # 9. 关闭所有MCP连接
            logger.info("关闭MCP连接...")
            try:
                if hasattr(app.state, "mcp_client_manager"):
                    await asyncio.wait_for(app.state.mcp_client_manager.shutdown_all(), timeout=2.0)
                    logger.info("MCP连接已关闭")
                else:
                    logger.info("MCP客户端管理器未初始化，跳过关闭")
            except TimeoutError:
                logger.warning("MCP连接关闭超时")
            except Exception as e:
                logger.warning(f"MCP连接关闭失败: {e}")

            # 10. 清理LLM服务
            logger.info("清理LLM服务...")
            try:
                await asyncio.wait_for(cleanup_llm_service(), timeout=1.0)
                logger.info("LLM服务已清理")
            except TimeoutError:
                logger.warning("LLM服务清理超时")
            except Exception as e:
                logger.warning(f"LLM服务清理失败: {e}")

            # 11. 关闭HTTP客户端
            logger.info("关闭HTTP客户端...")
            try:
                await asyncio.wait_for(HttpClientManager.close_all(), timeout=1.0)
                logger.info("HTTP客户端已关闭")
            except TimeoutError:
                logger.warning("HTTP客户端关闭超时")
            except Exception as e:
                logger.warning(f"HTTP客户端关闭失败: {e}")

            # 12. 强制清理连接
            logger.info("强制清理连接...")
            try:
                import gc
                gc.collect()
                await asyncio.sleep(0.05)
                logger.info("连接清理完成")
            except Exception as e:
                logger.warning(f"连接清理失败: {e}")

            # 13. 关闭豆包Embedding客户端
            logger.info("关闭豆包Embedding客户端...")
            try:
                from app.clients.doubao_embedding_client import close_doubao_embedding_client
                await asyncio.wait_for(close_doubao_embedding_client(), timeout=1.0)
                logger.info("豆包Embedding客户端已关闭")
            except TimeoutError:
                logger.warning("豆包Embedding客户端关闭超时")
            except Exception as e:
                logger.warning(f"豆包Embedding客户端关闭失败: {e}")

            # 14. 关闭数据库
            logger.info("关闭数据库连接...")
            try:
                from app.core.infrastructure.db.database import close_database
                await asyncio.wait_for(close_database(), timeout=1.0)
                logger.info("数据库已关闭")
            except TimeoutError:
                logger.warning("数据库关闭超时")
            except Exception as e:
                logger.warning(f"数据库关闭失败: {e}")

    except TimeoutError:
        logger.error(f"优雅关闭超时({shutdown_timeout}秒),强制退出")
    except Exception as e:
        logger.error(f"关闭过程异常: {e}")

    logger.info("系统已关闭")
