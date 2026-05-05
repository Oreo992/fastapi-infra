"""
全局线程池管理器

为计算密集型任务提供统一的线程池管理，避免创建过多线程导致资源耗尽
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Optional

from infra.logging import get_logger


logger = get_logger(__name__)


class GlobalThreadPoolManager:
    """全局线程池管理器（单例）"""

    _instance: Optional["GlobalThreadPoolManager"] = None
    _lock = asyncio.Lock()

    def __init__(self, config: dict = None):
        """初始化线程池
        
        Args:
            config: 配置字典，包含 compute_thread_pool_size 和 io_thread_pool_size
        """
        if GlobalThreadPoolManager._instance is not None:
            raise RuntimeError("请使用 GlobalThreadPoolManager.get_instance() 获取实例")

        self._config = config or {}

        # 计算密集型线程池（用于 CPU 密集任务）
        self._compute_pool_size = self._config.get("compute_thread_pool_size", 4)
        self._compute_executor: ThreadPoolExecutor | None = None

        # IO 密集型线程池（用于文件读写、网络请求等）
        self._io_pool_size = self._config.get("io_thread_pool_size", 10)
        self._io_executor: ThreadPoolExecutor | None = None

        self._initialized = False

        logger.info(
            f"GlobalThreadPoolManager 配置: "
            f"计算线程池={self._compute_pool_size}, "
            f"IO线程池={self._io_pool_size}"
        )

    @classmethod
    async def get_instance(cls, config: dict = None) -> "GlobalThreadPoolManager":
        """获取单例实例（线程安全）
        
        Args:
            config: 配置字典（仅第一次调用时生效）
        """
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(config)
                    await cls._instance.initialize()
        return cls._instance

    async def initialize(self):
        """初始化线程池"""
        if self._initialized:
            return

        try:
            # 创建计算线程池
            self._compute_executor = ThreadPoolExecutor(
                max_workers=self._compute_pool_size, thread_name_prefix="compute-"
            )

            # 创建 IO 线程池
            self._io_executor = ThreadPoolExecutor(
                max_workers=self._io_pool_size, thread_name_prefix="io-"
            )

            self._initialized = True
            logger.info("全局线程池初始化成功")

        except Exception as e:
            logger.error(f"全局线程池初始化失败: {e}", exc_info=True)
            raise

    def get_compute_executor(self) -> ThreadPoolExecutor:
        """获取计算线程池"""
        if not self._initialized or self._compute_executor is None:
            raise RuntimeError("线程池未初始化，请先调用 initialize()")
        return self._compute_executor

    def get_io_executor(self) -> ThreadPoolExecutor:
        """获取 IO 线程池"""
        if not self._initialized or self._io_executor is None:
            raise RuntimeError("线程池未初始化，请先调用 initialize()")
        return self._io_executor

    async def run_compute_task(self, func, *args, **kwargs):
        """
        在计算线程池中运行任务

        Args:
            func: 要执行的函数
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            函数执行结果
        """
        if not self._initialized:
            await self.initialize()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._compute_executor, lambda: func(*args, **kwargs)
        )

    async def run_io_task(self, func, *args, **kwargs):
        """
        在 IO 线程池中运行任务

        Args:
            func: 要执行的函数
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            函数执行结果
        """
        if not self._initialized:
            await self.initialize()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._io_executor, lambda: func(*args, **kwargs)
        )

    async def shutdown(self, wait: bool = True, timeout: float = 5.0):
        """
        关闭线程池

        Args:
            wait: 是否等待任务完成
            timeout: 超时时间(秒),超时后强制关闭
        """
        logger.info("开始关闭全局线程池...")

        async def shutdown_executor(executor, name: str):
            """关闭单个executor,带超时控制"""
            try:
                if not wait:
                    # 不等待,直接取消所有任务
                    executor.shutdown(wait=False, cancel_futures=True)
                    logger.info(f"{name}已取消所有任务并关闭")
                    return

                # 等待模式,但有超时
                loop = asyncio.get_event_loop()
                shutdown_task = loop.run_in_executor(
                    None, lambda: executor.shutdown(wait=True, cancel_futures=False)
                )

                try:
                    await asyncio.wait_for(shutdown_task, timeout=timeout)
                    logger.info(f"{name}正常关闭")
                except TimeoutError:
                    logger.warning(f"{name}关闭超时({timeout}秒),强制关闭")
                    executor.shutdown(wait=False, cancel_futures=True)

            except Exception as e:
                logger.error(f"{name}关闭失败: {e}")
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass

        # 并行关闭两个线程池
        tasks = []
        if self._compute_executor:
            tasks.append(shutdown_executor(self._compute_executor, "计算线程池"))
        if self._io_executor:
            tasks.append(shutdown_executor(self._io_executor, "IO线程池"))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._initialized = False
        logger.info("全局线程池关闭完成")

    def get_stats(self) -> dict:
        """获取线程池统计信息"""
        return {
            "initialized": self._initialized,
            "compute_pool": {
                "size": self._compute_pool_size,
                "active": self._compute_executor is not None,
            },
            "io_pool": {
                "size": self._io_pool_size,
                "active": self._io_executor is not None,
            },
        }


async def run_in_compute_pool(func, *args, **kwargs):
    """
    在计算线程池中运行函数（便捷方法）

    用于 CPU 密集型任务，如占星计算、数据处理等

    Example:
        result = await run_in_compute_pool(heavy_calculation, param1, param2)
    """
    manager = await GlobalThreadPoolManager.get_instance()
    return await manager.run_compute_task(func, *args, **kwargs)


async def run_in_io_pool(func, *args, **kwargs):
    """
    在 IO 线程池中运行函数（便捷方法）

    用于 IO 密集型任务，如文件读写、网络请求等

    Example:
        result = await run_in_io_pool(read_large_file, file_path)
    """
    manager = await GlobalThreadPoolManager.get_instance()
    return await manager.run_io_task(func, *args, **kwargs)


def compute_task(func):
    """
    装饰器：将同步函数标记为计算任务，自动在计算线程池中执行

    Example:
        @compute_task
        def heavy_calculation(x, y):
            return x ** y

        result = await heavy_calculation(2, 1000)
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        return await run_in_compute_pool(func, *args, **kwargs)

    return wrapper


def io_task(func):
    """
    装饰器：将同步函数标记为 IO 任务，自动在 IO 线程池中执行

    Example:
        @io_task
        def read_file(path):
            with open(path) as f:
                return f.read()

        content = await read_file('/path/to/file')
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        return await run_in_io_pool(func, *args, **kwargs)

    return wrapper


# 获取线程池管理器实例的便捷函数
async def get_thread_pool_manager() -> GlobalThreadPoolManager:
    """获取全局线程池管理器实例"""
    return await GlobalThreadPoolManager.get_instance()
