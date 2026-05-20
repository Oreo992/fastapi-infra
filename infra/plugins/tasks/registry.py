from typing import Any, cast

from infra.plugins.tasks.models import TaskEnvelope


class TaskQueueBackendRegistry:
    def __init__(self, default_provider: str = "memory") -> None:
        self.default_provider = default_provider
        self._providers: dict[str, Any] = {}

    def register(self, provider: Any, *, default: bool = False) -> None:
        self._providers[provider.name] = provider
        if default:
            self.default_provider = provider.name

    def provider(self, name: str | None = None) -> Any:
        provider_name = name or self.default_provider
        provider = self._providers.get(provider_name)
        if provider is None:
            raise LookupError(f"unknown task queue provider: {provider_name}")
        return provider

    async def enqueue(
        self,
        name: str,
        payload: dict[str, object] | None = None,
        *,
        idempotency_key: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int = 1,
    ) -> TaskEnvelope:
        return cast(
            TaskEnvelope,
            await self.provider().enqueue(
                name,
                payload,
                idempotency_key=idempotency_key,
                delay_seconds=delay_seconds,
                max_attempts=max_attempts,
            ),
        )

    async def dequeue(self) -> TaskEnvelope | None:
        return cast(TaskEnvelope | None, await self.provider().dequeue())

    async def complete(self, task_id: str) -> None:
        await self.provider().complete(task_id)

    async def fail(self, task_id: str, reason: str) -> None:
        await self.provider().fail(task_id, reason)

    async def retry(
        self,
        task_id: str,
        reason: str,
        *,
        delay_seconds: float = 0,
    ) -> None:
        await self.provider().retry(task_id, reason, delay_seconds=delay_seconds)

    async def dead_letter(self, task_id: str, reason: str) -> None:
        await self.provider().dead_letter(task_id, reason)

    def get(self, task_id: str) -> TaskEnvelope:
        return cast(TaskEnvelope, self.provider().get(task_id))

    def names(self) -> list[str]:
        return sorted(self._providers)
