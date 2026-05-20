"""Saga transaction coordinator for cross-system workflows."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from infra.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Operation:
    """A single Saga step."""

    name: str
    execute: Callable[[], Awaitable[Any]]
    compensate: Callable[[], Awaitable[None]] | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class OperationFailure:
    """A failed operation or compensation step."""

    operation: str
    error: Exception


@dataclass
class TransactionResult:
    """Result of a Saga transaction run."""

    success: bool
    completed_operations: list[str]
    results: dict[str, Any] = field(default_factory=dict)
    failed_operations: list[OperationFailure] = field(default_factory=list)
    compensated_operations: list[str] = field(default_factory=list)
    compensation_failures: list[OperationFailure] = field(default_factory=list)
    error: Exception | None = None

    @property
    def failed_operation(self) -> str | None:
        """Return the first failed operation name for compact status reporting."""
        if not self.failed_operations:
            return None
        return self.failed_operations[0].operation

    @property
    def compensation_succeeded(self) -> bool:
        """Whether all attempted compensation steps completed."""
        return not self.compensation_failures


class TransactionCoordinator:
    """Saga coordinator with explicit execution and compensation reports."""

    async def execute_with_compensation(
        self, operations: list[Operation], *, continue_on_error: bool = False
    ) -> TransactionResult:
        """Execute operations in order and compensate completed steps on failure.

        Args:
            operations: Steps to execute in order.
            continue_on_error: Keep running later steps after a failure. When true,
                compensation is not attempted automatically because later operations
                may depend on the successful subset.

        Returns:
            Detailed transaction report.
        """
        self._validate_operations(operations)

        completed: list[str] = []
        results: dict[str, Any] = {}
        failed: list[OperationFailure] = []
        compensation_stack: list[Operation] = []

        try:
            for op in operations:
                logger.info(f"执行操作: {op.name}")

                try:
                    result = await self._execute_operation(op)
                    completed.append(op.name)
                    results[op.name] = result

                    if op.compensate:
                        compensation_stack.append(op)

                except Exception as e:
                    logger.error(f"操作失败: {op.name}, 错误: {e}")
                    failed.append(OperationFailure(operation=op.name, error=e))

                    if not continue_on_error:
                        compensation_report = await self._compensate(compensation_stack)

                        return TransactionResult(
                            success=False,
                            completed_operations=completed,
                            results=results,
                            failed_operations=failed,
                            compensated_operations=compensation_report[0],
                            compensation_failures=compensation_report[1],
                            error=e,
                        )

                    logger.warning(f"忽略错误，继续执行: {op.name}")

            return TransactionResult(
                success=not failed,
                completed_operations=completed,
                results=results,
                failed_operations=failed,
                error=failed[0].error if failed else None,
            )

        except Exception as e:
            logger.error(f"事务执行异常: {e}", exc_info=True)
            compensation_report = await self._compensate(compensation_stack)

            return TransactionResult(
                success=False,
                completed_operations=completed,
                results=results,
                compensated_operations=compensation_report[0],
                compensation_failures=compensation_report[1],
                error=e,
            )

    async def _execute_operation(self, operation: Operation) -> Any:
        if operation.timeout_seconds is None:
            return await operation.execute()
        return await asyncio.wait_for(operation.execute(), timeout=operation.timeout_seconds)

    async def _compensate(
        self, compensation_stack: list[Operation]
    ) -> tuple[list[str], list[OperationFailure]]:
        """Run compensation steps in reverse order."""

        if not compensation_stack:
            return [], []

        logger.warning(f"开始执行补偿操作: {len(compensation_stack)}个")
        compensated: list[str] = []
        failures: list[OperationFailure] = []

        for op in reversed(compensation_stack):
            try:
                logger.info(f"补偿操作: {op.name}")
                if op.compensate is not None:
                    await op.compensate()
                    compensated.append(op.name)
            except Exception as e:
                logger.error(f"补偿操作失败: {op.name}, 错误: {e}", exc_info=True)
                failures.append(OperationFailure(operation=op.name, error=e))

        logger.info("补偿操作完成")
        return compensated, failures

    def _validate_operations(self, operations: list[Operation]) -> None:
        names: set[str] = set()
        for op in operations:
            if not op.name.strip():
                raise ValueError("operation name must not be empty")
            if op.name in names:
                raise ValueError(f"duplicate operation name: {op.name}")
            names.add(op.name)
            if op.timeout_seconds is not None and op.timeout_seconds <= 0:
                raise ValueError(f"operation timeout must be positive: {op.name}")
