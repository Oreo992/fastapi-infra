import asyncio
from collections.abc import Callable
from typing import Any

from infra.plugins.tasks.adapters._broker import BrokerMessage, BrokerTaskQueue
from infra.plugins.tasks.models import TaskEnvelope


class KafkaTaskQueue(BrokerTaskQueue):
    name = "kafka"

    def __init__(
        self,
        *,
        topic: str,
        bootstrap_servers: str | list[str],
        group_id: str,
        producer: Any | None = None,
        consumer: Any | None = None,
        client_id: str = "fastapi-infra-tasks",
        dead_letter_topic: str | None = None,
        poll_timeout_seconds: float = 1.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(now=now)
        self._topic = topic
        self._bootstrap_servers = bootstrap_servers
        self._group_id = group_id
        self._producer = producer
        self._consumer = consumer
        self._client_id = client_id
        self._dead_letter_topic = dead_letter_topic
        self._poll_timeout_seconds = poll_timeout_seconds
        self._started = False

    async def _send(self, task: TaskEnvelope, *, delay_seconds: float = 0) -> None:
        await self._ensure_started()
        producer = self._producer
        assert producer is not None
        await producer.send_and_wait(
            self._topic,
            task.model_dump_json().encode(),
            key=task.id.encode(),
        )

    async def _receive(self) -> BrokerMessage | None:
        await self._ensure_started()
        consumer = self._consumer
        assert consumer is not None
        try:
            message = await asyncio.wait_for(
                consumer.getone(),
                timeout=self._poll_timeout_seconds,
            )
        except TimeoutError:
            return None
        task = TaskEnvelope.model_validate_json(_message_value(message))
        return BrokerMessage(task=task, receipt=message)

    async def _ack(self, receipt: Any) -> None:
        commit = getattr(self._consumer, "commit", None)
        if commit is None:
            return None
        result = commit()
        if asyncio.iscoroutine(result):
            await result
        return None

    async def _send_dead_letter(self, task: TaskEnvelope) -> None:
        if self._dead_letter_topic is None:
            return None
        await self._ensure_started()
        producer = self._producer
        assert producer is not None
        await producer.send_and_wait(
            self._dead_letter_topic,
            task.model_dump_json().encode(),
            key=task.id.encode(),
        )
        return None

    async def _health_check(self) -> bool:
        await self._ensure_started()
        return True

    async def close(self) -> None:
        for client in (self._consumer, self._producer):
            stop = getattr(client, "stop", None)
            if stop is None:
                continue
            result = stop()
            if asyncio.iscoroutine(result):
                await result
        self._started = False

    async def _ensure_started(self) -> None:
        if self._started:
            return
        if self._producer is None or self._consumer is None:
            self._producer, self._consumer = _create_aiokafka_clients(
                topic=self._topic,
                bootstrap_servers=self._bootstrap_servers,
                group_id=self._group_id,
                client_id=self._client_id,
            )
        for client in (self._producer, self._consumer):
            start = getattr(client, "start", None)
            if start is None:
                continue
            result = start()
            if asyncio.iscoroutine(result):
                await result
        self._started = True


def _message_value(message: Any) -> bytes | str:
    value = getattr(message, "value", message)
    if isinstance(value, bytes | str):
        return value
    return bytes(value)


def _create_aiokafka_clients(
    *,
    topic: str,
    bootstrap_servers: str | list[str],
    group_id: str,
    client_id: str,
) -> tuple[Any, Any]:
    try:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("Kafka task backend requires the 'aiokafka' package") from exc
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        client_id=client_id,
    )
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap_servers,
        group_id=group_id,
        client_id=client_id,
        enable_auto_commit=False,
    )
    return producer, consumer
