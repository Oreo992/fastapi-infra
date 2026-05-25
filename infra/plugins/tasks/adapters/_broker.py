import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from infra.plugins.tasks.models import TaskEnvelope, normalize_idempotency_key


@dataclass(frozen=True)
class BrokerMessage:
    task: TaskEnvelope
    receipt: Any


class BrokerTaskQueue:
    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.time
        self._tasks: dict[str, TaskEnvelope] = {}
        self._receipts: dict[str, Any] = {}
        self._idempotency_keys: dict[str, str] = {}

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int = 1,
    ) -> TaskEnvelope:
        normalized_key = normalize_idempotency_key(idempotency_key)
        if normalized_key is not None:
            existing_id = self._idempotency_keys.get(normalized_key)
            if existing_id is not None:
                return self.get(existing_id)

        task = TaskEnvelope(
            name=name,
            payload=payload,
            idempotency_key=normalized_key,
            max_attempts=max_attempts,
            available_at=self._now() + max(0, delay_seconds),
        )
        self._cache_task(task)
        if normalized_key is not None:
            self._idempotency_keys[normalized_key] = task.id
        await self._send(task, delay_seconds=max(0, delay_seconds))
        return task.model_copy(deep=True)

    async def dequeue(self) -> TaskEnvelope | None:
        message = await self._receive()
        if message is None:
            return None

        task = message.task
        self._receipts[task.id] = message.receipt
        if task.state not in ("queued", "running"):
            await self._ack(message.receipt)
            self._receipts.pop(task.id, None)
            return None
        if task.available_at > self._now():
            await self._defer(message)
            return None

        running = task.model_copy(update={"state": "running", "attempts": task.attempts + 1})
        self._cache_task(running)
        return running.model_copy(deep=True)

    async def complete(self, task_id: str) -> None:
        task = self._tasks[task_id]
        self._cache_task(task.model_copy(update={"state": "completed", "error": None}))
        await self._ack_task(task_id)

    async def fail(self, task_id: str, reason: str) -> None:
        task = self._tasks[task_id]
        self._cache_task(task.model_copy(update={"state": "failed", "error": reason}))
        await self._ack_task(task_id)

    async def retry(
        self,
        task_id: str,
        reason: str,
        *,
        delay_seconds: float = 0,
    ) -> None:
        task = self._tasks[task_id]
        retried = task.model_copy(
            update={
                "state": "queued",
                "error": reason,
                "available_at": self._now() + max(0, delay_seconds),
            }
        )
        self._cache_task(retried)
        await self._send(retried, delay_seconds=max(0, delay_seconds))
        await self._ack_task(task_id)

    async def dead_letter(self, task_id: str, reason: str) -> None:
        task = self._tasks[task_id]
        dead = task.model_copy(update={"state": "dead_lettered", "error": reason})
        self._cache_task(dead)
        await self._send_dead_letter(dead)
        await self._ack_task(task_id)

    async def health_check(self) -> bool:
        return await self._health_check()

    async def close(self) -> None:
        return None

    def get(self, task_id: str) -> TaskEnvelope:
        return self._tasks[task_id].model_copy(deep=True)

    async def _defer(self, message: BrokerMessage) -> None:
        remaining = max(0, message.task.available_at - self._now())
        await self._send(message.task, delay_seconds=remaining)
        await self._ack(message.receipt)
        self._receipts.pop(message.task.id, None)

    async def _ack_task(self, task_id: str) -> None:
        receipt = self._receipts.pop(task_id, None)
        if receipt is not None:
            await self._ack(receipt)

    def _cache_task(self, task: TaskEnvelope) -> None:
        self._tasks[task.id] = task.model_copy(deep=True)

    async def _send(self, task: TaskEnvelope, *, delay_seconds: float = 0) -> None:
        raise NotImplementedError

    async def _receive(self) -> BrokerMessage | None:
        raise NotImplementedError

    async def _ack(self, receipt: Any) -> None:
        raise NotImplementedError

    async def _send_dead_letter(self, task: TaskEnvelope) -> None:
        return None

    async def _health_check(self) -> bool:
        raise NotImplementedError
