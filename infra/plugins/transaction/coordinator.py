"""事务协调器

实现Saga补偿模式，协调跨多个存储系统的事务一致性
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from infra.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Operation:
    """操作定义"""

    name: str
    execute: Callable[[], Awaitable[Any]]
    compensate: Callable[[], Awaitable[None]] | None = None


@dataclass
class TransactionResult:
    """事务结果"""

    success: bool
    completed_operations: list[str]
    failed_operation: str | None = None
    error: Exception | None = None
    data: Any = None


class TransactionCoordinator:
    """事务协调器 - Saga补偿模式"""

    async def execute_with_compensation(
        self, operations: list[Operation], stop_on_error: bool = True
    ) -> TransactionResult:
        """执行操作序列，失败时自动补偿

        Args:
            operations: 操作列表（按顺序执行）
            stop_on_error: 遇到错误是否立即停止

        Returns:
            TransactionResult
        """
        completed = []
        compensation_stack = []

        try:
            # 执行所有操作
            for op in operations:
                logger.info(f"执行操作: {op.name}")

                try:
                    result = await op.execute()
                    completed.append(op.name)

                    # 记录补偿操作
                    if op.compensate:
                        compensation_stack.append(op)

                except Exception as e:
                    logger.error(f"操作失败: {op.name}, 错误: {e}")

                    if stop_on_error:
                        # 执行补偿
                        await self._compensate(compensation_stack)

                        return TransactionResult(
                            success=False,
                            completed_operations=completed,
                            failed_operation=op.name,
                            error=e,
                        )
                    else:
                        # 继续执行
                        logger.warning(f"忽略错误，继续执行: {op.name}")

            return TransactionResult(success=True, completed_operations=completed)

        except Exception as e:
            logger.error(f"事务执行异常: {e}", exc_info=True)
            await self._compensate(compensation_stack)

            return TransactionResult(
                success=False, completed_operations=completed, error=e
            )

    async def _compensate(self, compensation_stack: list[Operation]):
        """执行补偿操作（逆序）

        Args:
            compensation_stack: 需要补偿的操作列表
        """
        if not compensation_stack:
            return

        logger.warning(f"开始执行补偿操作: {len(compensation_stack)}个")

        # 逆序执行补偿
        for op in reversed(compensation_stack):
            try:
                logger.info(f"补偿操作: {op.name}")
                await op.compensate()
            except Exception as e:
                logger.error(f"补偿操作失败: {op.name}, 错误: {e}", exc_info=True)
                # 补偿失败不中断，继续尝试其他补偿

        logger.info("补偿操作完成")


class MusicSyncTransactionCoordinator:
    """音乐同步专用事务协调器

    协调MySQL和Milvus的两阶段操作，确保数据一致性
    """

    def __init__(self):
        self.coordinator = TransactionCoordinator()

    async def execute_sync_transaction(
        self, sync_operations: dict[str, Any]
    ) -> TransactionResult:
        """执行同步事务

        Args:
            sync_operations: 同步操作定义，包含：
                - mysql_execute: MySQL操作执行函数
                - mysql_rollback: MySQL回滚函数
                - vector_execute: 向量操作执行函数
                - vector_rollback: 向量回滚函数

        Returns:
            TransactionResult
        """
        operations = []

        # 1. MySQL操作
        if "mysql_execute" in sync_operations:
            operations.append(
                Operation(
                    name="MySQL操作",
                    execute=sync_operations["mysql_execute"],
                    compensate=sync_operations.get("mysql_rollback"),
                )
            )

        # 2. Milvus操作
        if "vector_execute" in sync_operations:
            operations.append(
                Operation(
                    name="Milvus向量操作",
                    execute=sync_operations["vector_execute"],
                    compensate=sync_operations.get("vector_rollback"),
                )
            )

        # 3. 搜索字段更新
        if "search_execute" in sync_operations:
            operations.append(
                Operation(
                    name="搜索字段更新",
                    execute=sync_operations["search_execute"],
                    compensate=None,  # 搜索字段更新失败不需要回滚
                )
            )

        # 4. 缓存更新
        if "cache_execute" in sync_operations:
            operations.append(
                Operation(
                    name="缓存更新",
                    execute=sync_operations["cache_execute"],
                    compensate=None,  # 缓存更新失败不需要回滚
                )
            )

        # 执行事务
        result = await self.coordinator.execute_with_compensation(operations)

        if not result.success:
            logger.error(
                f"同步事务失败: {result.failed_operation}, "
                f"已完成: {result.completed_operations}"
            )
        else:
            logger.info(f"同步事务成功: 完成{len(result.completed_operations)}个操作")

        return result
