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

