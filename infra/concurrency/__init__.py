"""并发控制模块

提供线程池管理和装饰器：
- GlobalThreadPoolManager: 全局线程池单例
- run_in_compute_pool: 计算密集型任务装饰器
- run_in_io_pool: IO密集型任务装饰器
"""

from infra.concurrency.thread_pool import (
    GlobalThreadPoolManager,
    run_in_compute_pool,
    run_in_io_pool,
)

__all__ = [
    "GlobalThreadPoolManager",
    "run_in_compute_pool",
    "run_in_io_pool",
]
