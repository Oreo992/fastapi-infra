"""
应用生命周期管理

提供 FastAPI 应用的启动和关闭钩子注册机制
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI

from infra.logging import get_logger

logger = get_logger(__name__)


# 回调函数类型
StartupCallback = Callable[[], Awaitable[None]]
ShutdownCallback = Callable[[], Awaitable[None]]


class LifecycleManager:
    """应用生命周期管理器"""

    def __init__(self, app: FastAPI):
        """初始化生命周期管理器
        
        Args:
            app: FastAPI 应用实例
        """
        self.app = app
        self._startup_callbacks: list[StartupCallback] = []
        self._shutdown_callbacks: list[ShutdownCallback] = []
        self._registered = False

    def add_startup_callback(self, callback: StartupCallback, name: str = None):
        """添加启动回调
        
        Args:
            callback: 启动时执行的异步函数
            name: 回调名称（用于日志）
        """
        if not name:
            name = callback.__name__
        
        async def wrapped_callback():
            try:
                logger.info(f"执行启动回调: {name}")
                await callback()
                logger.info(f"启动回调完成: {name}")
            except Exception as e:
                logger.error(f"启动回调失败: {name}", exc_info=True)
                raise
        
        self._startup_callbacks.append(wrapped_callback)

    def add_shutdown_callback(self, callback: ShutdownCallback, name: str = None):
        """添加关闭回调
        
        Args:
            callback: 关闭时执行的异步函数
            name: 回调名称（用于日志）
        """
        if not name:
            name = callback.__name__
        
        async def wrapped_callback():
            try:
                logger.info(f"执行关闭回调: {name}")
                await callback()
                logger.info(f"关闭回调完成: {name}")
            except Exception as e:
                logger.error(f"关闭回调失败: {name}", exc_info=True)
                # 关闭时不抛出异常，避免中断其他清理
        
        self._shutdown_callbacks.append(wrapped_callback)

    def register(self):
        """注册生命周期回调到 FastAPI 应用"""
        if self._registered:
            logger.warning("生命周期管理器已注册，跳过重复注册")
            return

        @self.app.on_event("startup")
        async def startup_event():
            """应用启动事件"""
            logger.info(f"应用启动中，共 {len(self._startup_callbacks)} 个启动回调...")
            for callback in self._startup_callbacks:
                await callback()
            logger.info("应用启动完成")

        @self.app.on_event("shutdown")
        async def shutdown_event():
            """应用关闭事件"""
            logger.info(f"应用关闭中，共 {len(self._shutdown_callbacks)} 个关闭回调...")
            for callback in self._shutdown_callbacks:
                await callback()
            logger.info("应用关闭完成")

        self._registered = True
        logger.info("生命周期管理器已注册")


def create_lifecycle_manager(app: FastAPI) -> LifecycleManager:
    """创建生命周期管理器的快捷函数
    
    Args:
        app: FastAPI 应用实例
        
    Returns:
        配置好的生命周期管理器
    """
    return LifecycleManager(app)
