from typing import Protocol

from infra.plugins.tasks.models import TaskEnvelope


class TaskQueue(Protocol):
    async def enqueue(
        self,
        name: str,
        payload: dict[str, object] | None = None,
    ) -> TaskEnvelope:
        raise NotImplementedError

    async def dequeue(self) -> TaskEnvelope | None:
        raise NotImplementedError

    async def complete(self, task_id: str) -> None:
        raise NotImplementedError

    async def fail(self, task_id: str, reason: str) -> None:
        raise NotImplementedError

    def get(self, task_id: str) -> TaskEnvelope:
        raise NotImplementedError
