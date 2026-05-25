import asyncio
import socket
from collections.abc import Callable
from typing import Any

from infra.plugins.tasks.adapters._broker import BrokerMessage, BrokerTaskQueue
from infra.plugins.tasks.models import TaskEnvelope


class CeleryTaskQueue(BrokerTaskQueue):
    name = "celery"

    def __init__(
        self,
        *,
        broker_url: str,
        queue_name: str = "infra.tasks",
        exchange_name: str = "infra.tasks",
        routing_key: str = "infra.tasks",
        dead_letter_queue_name: str | None = None,
        transport: Any | None = None,
        poll_timeout_seconds: float = 1.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(now=now)
        self._transport = transport or _KombuTaskTransport(
            broker_url=broker_url,
            queue_name=queue_name,
            exchange_name=exchange_name,
            routing_key=routing_key,
            poll_timeout_seconds=poll_timeout_seconds,
        )
        self._dead_letter_queue_name = dead_letter_queue_name

    async def _send(self, task: TaskEnvelope, *, delay_seconds: float = 0) -> None:
        await self._transport.publish(task)

    async def _receive(self) -> BrokerMessage | None:
        return await self._transport.receive()

    async def _ack(self, receipt: Any) -> None:
        await self._transport.ack(receipt)

    async def _send_dead_letter(self, task: TaskEnvelope) -> None:
        if self._dead_letter_queue_name is None:
            return None
        await self._transport.publish_dead_letter(task, self._dead_letter_queue_name)
        return None

    async def _health_check(self) -> bool:
        return await self._transport.health_check()

    async def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


class _KombuTaskTransport:
    def __init__(
        self,
        *,
        broker_url: str,
        queue_name: str,
        exchange_name: str,
        routing_key: str,
        poll_timeout_seconds: float,
    ) -> None:
        try:
            from kombu import Connection, Exchange, Queue  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("Celery task backend requires the 'celery' package") from exc
        self._connection = Connection(broker_url)
        self._exchange = Exchange(exchange_name, type="direct", durable=True)
        self._queue = Queue(queue_name, self._exchange, routing_key=routing_key, durable=True)
        self._routing_key = routing_key
        self._poll_timeout_seconds = poll_timeout_seconds

    async def publish(self, task: TaskEnvelope) -> None:
        await asyncio.to_thread(self._publish_sync, task, self._queue, self._routing_key)

    async def publish_dead_letter(self, task: TaskEnvelope, queue_name: str) -> None:
        queue = self._queue.clone()
        queue.name = queue_name
        await asyncio.to_thread(self._publish_sync, task, queue, queue_name)

    async def receive(self) -> BrokerMessage | None:
        return await asyncio.to_thread(self._receive_sync)

    async def ack(self, receipt: Any) -> None:
        await asyncio.to_thread(receipt.ack)

    async def health_check(self) -> bool:
        await asyncio.to_thread(self._connection.ensure_connection, max_retries=1)
        return True

    def close(self) -> None:
        self._connection.release()

    def _publish_sync(self, task: TaskEnvelope, queue: Any, routing_key: str) -> None:
        from kombu import Producer  # type: ignore[import-not-found]

        self._connection.ensure_connection(max_retries=3)
        producer = Producer(self._connection)
        producer.publish(
            task.model_dump(mode="json"),
            exchange=queue.exchange,
            routing_key=routing_key,
            declare=[queue],
            serializer="json",
            delivery_mode=2,
        )

    def _receive_sync(self) -> BrokerMessage | None:
        received: list[BrokerMessage] = []

        def callback(body: dict[str, Any], message: Any) -> None:
            received.append(BrokerMessage(task=TaskEnvelope.model_validate(body), receipt=message))

        from kombu import Consumer  # type: ignore[import-not-found]

        with Consumer(
            self._connection,
            queues=[self._queue],
            callbacks=[callback],
            accept=["json"],
        ):
            try:
                self._connection.drain_events(timeout=self._poll_timeout_seconds)
            except (TimeoutError, OSError, socket.timeout):
                return None
        return received[0] if received else None
