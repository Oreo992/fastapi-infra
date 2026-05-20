from typing import Protocol, runtime_checkable

from infra.plugins.tasks.models import TaskEnvelope


@runtime_checkable
class TaskQueue(Protocol):
    async def enqueue(
        self,
        name: str,
        payload: dict[str, object] | None = None,
        *,
        idempotency_key: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int = 1,
    ) -> TaskEnvelope:
        raise NotImplementedError

    async def dequeue(self) -> TaskEnvelope | None:
        raise NotImplementedError

    async def complete(self, task_id: str) -> None:
        raise NotImplementedError

    async def fail(self, task_id: str, reason: str) -> None:
        raise NotImplementedError

    async def retry(
        self,
        task_id: str,
        reason: str,
        *,
        delay_seconds: float = 0,
    ) -> None:
        raise NotImplementedError

    async def dead_letter(self, task_id: str, reason: str) -> None:
        raise NotImplementedError

    def get(self, task_id: str) -> TaskEnvelope:
        raise NotImplementedError
