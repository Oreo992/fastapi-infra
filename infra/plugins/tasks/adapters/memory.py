import time
from collections import deque
from collections.abc import Callable
from typing import Any

from infra.plugins.tasks.models import TaskEnvelope


class MemoryTaskQueue:
    name = "memory"

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._queued: deque[str] = deque()
        self._tasks: dict[str, TaskEnvelope] = {}
        self._idempotency_keys: dict[str, str] = {}
        self._now = now or time.time

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int = 1,
    ) -> TaskEnvelope:
        normalized_key = _normalize_idempotency_key(idempotency_key)
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
        self._tasks[task.id] = task
        if normalized_key is not None:
            self._idempotency_keys[normalized_key] = task.id
        self._queued.append(task.id)
        return task.model_copy(deep=True)

    async def dequeue(self) -> TaskEnvelope | None:
        now = self._now()
        for _ in range(len(self._queued)):
            task_id = self._queued.popleft()
            task = self._tasks.get(task_id)
            if task is None or task.state != "queued":
                continue
            if task.available_at > now:
                self._queued.append(task_id)
                continue
            task.state = "running"
            task.attempts += 1
            return task.model_copy(deep=True)
        return None

    async def complete(self, task_id: str) -> None:
        task = self._tasks[task_id]
        task.state = "completed"
        task.error = None

    async def fail(self, task_id: str, reason: str) -> None:
        task = self._tasks[task_id]
        task.state = "failed"
        task.error = reason

    async def retry(
        self,
        task_id: str,
        reason: str,
        *,
        delay_seconds: float = 0,
    ) -> None:
        task = self._tasks[task_id]
        task.state = "queued"
        task.error = reason
        task.available_at = self._now() + max(0, delay_seconds)
        self._queued.append(task_id)

    async def dead_letter(self, task_id: str, reason: str) -> None:
        task = self._tasks[task_id]
        task.state = "dead_lettered"
        task.error = reason

    async def health_check(self) -> bool:
        return True

    def get(self, task_id: str) -> TaskEnvelope:
        return self._tasks[task_id].model_copy(deep=True)


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError("idempotency_key must not be empty")
    return normalized
