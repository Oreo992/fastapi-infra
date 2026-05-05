from collections import deque
from typing import Any

from infra.plugins.tasks.models import TaskEnvelope


class MemoryTaskQueue:
    def __init__(self) -> None:
        self._queued: deque[str] = deque()
        self._tasks: dict[str, TaskEnvelope] = {}

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
    ) -> TaskEnvelope:
        task = TaskEnvelope(name=name, payload=payload)
        self._tasks[task.id] = task
        self._queued.append(task.id)
        return task.model_copy(deep=True)

    async def dequeue(self) -> TaskEnvelope | None:
        while self._queued:
            task_id = self._queued.popleft()
            task = self._tasks.get(task_id)
            if task is None or task.state != "queued":
                continue
            task.state = "running"
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

    def get(self, task_id: str) -> TaskEnvelope:
        return self._tasks[task_id].model_copy(deep=True)
