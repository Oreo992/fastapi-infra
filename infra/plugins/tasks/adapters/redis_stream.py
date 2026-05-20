import inspect
import json
import time
from collections.abc import Callable
from typing import Any

from infra.plugins.tasks.models import TaskEnvelope


class RedisStreamTaskQueue:
    name = "redis"

    def __init__(
        self,
        redis: Any,
        stream_name: str = "infra:tasks",
        consumer_group: str = "infra",
        consumer_name: str = "tasks",
        pending_min_idle_ms: int = 60_000,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._redis = redis
        self._stream_name = stream_name
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name
        self._pending_min_idle_ms = pending_min_idle_ms
        self._now = now or time.time
        self._consumer_group_ready = False
        self._tasks: dict[str, TaskEnvelope] = {}
        self._message_ids: dict[str, str] = {}

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
            existing_id = await self._load_idempotent_task_id(normalized_key)
            if existing_id is not None:
                return await self.get_async(existing_id)

        task = TaskEnvelope(
            name=name,
            payload=payload,
            idempotency_key=normalized_key,
            max_attempts=max_attempts,
            available_at=self._now() + max(0, delay_seconds),
        )
        if normalized_key is not None:
            reserved = await self._reserve_idempotency_key(normalized_key, task.id)
            if not reserved:
                existing_id = await self._load_idempotent_task_id(normalized_key)
                if existing_id is not None:
                    return await self.get_async(existing_id)
        await self._persist(task)
        await self._publish(task.id)
        return task.model_copy(deep=True)

    async def dequeue(self) -> TaskEnvelope | None:
        await self._ensure_consumer_group()

        pending = await self._claim_pending()
        if pending is not None:
            return pending

        messages = await self._call(
            "xreadgroup",
            groupname=self._consumer_group,
            consumername=self._consumer_name,
            streams={self._stream_name: ">"},
            count=1,
        )
        return await self._consume_messages(messages)

    async def get_async(self, task_id: str) -> TaskEnvelope:
        """Load task state from Redis and refresh the sync get() cache."""
        return (await self._load(task_id)).model_copy(deep=True)

    def get(self, task_id: str) -> TaskEnvelope:
        return self._tasks[task_id].model_copy(deep=True)

    async def _ensure_consumer_group(self) -> None:
        if self._consumer_group_ready:
            return

        try:
            await self._call(
                "xgroup_create",
                self._stream_name,
                self._consumer_group,
                "0",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._consumer_group_ready = True

    async def _claim_pending(self) -> TaskEnvelope | None:
        if hasattr(self._redis, "xautoclaim"):
            result = await self._call(
                "xautoclaim",
                self._stream_name,
                self._consumer_group,
                self._consumer_name,
                self._pending_min_idle_ms,
                "0-0",
                count=1,
            )
            messages = self._parse_xautoclaim_messages(result)
            return await self._consume_messages([(self._stream_name, messages)])

        if hasattr(self._redis, "xpending_range") and hasattr(self._redis, "xclaim"):
            pending = await self._call(
                "xpending_range",
                self._stream_name,
                self._consumer_group,
                min="-",
                max="+",
                count=1,
            )
            message_ids = [
                message_id
                for message_id, idle_ms in self._parse_pending_entries(pending)
                if idle_ms >= self._pending_min_idle_ms
            ]
            if not message_ids:
                return None
            messages = await self._call(
                "xclaim",
                self._stream_name,
                self._consumer_group,
                self._consumer_name,
                self._pending_min_idle_ms,
                message_ids,
            )
            return await self._consume_messages([(self._stream_name, messages)])

        return None

    async def _consume_messages(
        self,
        messages: list[tuple[Any, list[tuple[Any, dict[Any, Any]]]]],
    ) -> TaskEnvelope | None:
        if not messages:
            return None

        for _stream, stream_messages in messages:
            for message_id, fields in stream_messages:
                normalized = self._normalize_mapping(fields)
                task_id = self._json_loads(normalized["task_id"])
                self._message_ids[task_id] = self._decode(message_id)
                task = await self._load(task_id)
                if task.state not in ("queued", "running"):
                    await self._ack(task_id)
                    self._message_ids.pop(task_id, None)
                    continue
                if task.available_at > self._now():
                    await self._ack(task_id)
                    self._message_ids.pop(task_id, None)
                    await self._publish(task_id)
                    continue
                running = task.model_copy(update={"state": "running"})
                running = running.model_copy(update={"attempts": running.attempts + 1})
                await self._persist(running)
                return running.model_copy(deep=True)
        return None

    async def complete(self, task_id: str) -> None:
        task = await self._load(task_id)
        completed = task.model_copy(update={"state": "completed", "error": None})
        await self._persist(completed)
        await self._ack(task_id)

    async def fail(self, task_id: str, reason: str) -> None:
        task = await self._load(task_id)
        failed = task.model_copy(update={"state": "failed", "error": reason})
        await self._persist(failed)
        await self._ack(task_id)

    async def retry(
        self,
        task_id: str,
        reason: str,
        *,
        delay_seconds: float = 0,
    ) -> None:
        task = await self._load(task_id)
        retried = task.model_copy(
            update={
                "state": "queued",
                "error": reason,
                "available_at": self._now() + max(0, delay_seconds),
            }
        )
        await self._persist(retried)
        await self._ack(task_id)
        self._message_ids.pop(task_id, None)
        await self._publish(task_id)

    async def dead_letter(self, task_id: str, reason: str) -> None:
        task = await self._load(task_id)
        dead_lettered = task.model_copy(update={"state": "dead_lettered", "error": reason})
        await self._persist(dead_lettered)
        await self._ack(task_id)

    async def health_check(self) -> bool:
        if hasattr(self._redis, "ping"):
            return bool(await self._call("ping"))
        await self._ensure_consumer_group()
        return True

    async def _persist(self, task: TaskEnvelope) -> None:
        mapping = {
            "id": self._json_dumps(task.id),
            "name": self._json_dumps(task.name),
            "payload": self._json_dumps(task.payload),
            "idempotency_key": self._json_dumps(task.idempotency_key),
            "state": self._json_dumps(task.state),
            "error": self._json_dumps(task.error),
            "attempts": self._json_dumps(task.attempts),
            "max_attempts": self._json_dumps(task.max_attempts),
            "available_at": self._json_dumps(task.available_at),
        }
        message_id = self._message_ids.get(task.id)
        if message_id is not None:
            mapping["_stream_message_id"] = self._json_dumps(message_id)
        await self._call("hset", self._task_key(task.id), mapping=mapping)
        self._tasks[task.id] = task.model_copy(deep=True)

    async def _load(self, task_id: str) -> TaskEnvelope:
        raw = await self._call("hgetall", self._task_key(task_id))
        data = self._normalize_mapping(raw)
        task = TaskEnvelope(
            id=self._json_loads(data["id"]),
            name=self._json_loads(data["name"]),
            payload=self._json_loads(data["payload"]),
            idempotency_key=self._json_loads(data.get("idempotency_key", "null")),
            state=self._json_loads(data["state"]),
            error=self._json_loads(data["error"]),
            attempts=self._json_loads(data["attempts"]),
            max_attempts=self._json_loads(data["max_attempts"]),
            available_at=self._json_loads(data["available_at"]),
        )
        message_id = data.get("_stream_message_id")
        if message_id is not None:
            self._message_ids[task.id] = self._json_loads(message_id)
        self._tasks[task.id] = task.model_copy(deep=True)
        return task

    async def _ack(self, task_id: str) -> None:
        message_id = self._message_ids.get(task_id)
        if message_id is None:
            return
        await self._call(
            "xack",
            self._stream_name,
            self._consumer_group,
            message_id,
        )

    async def _publish(self, task_id: str) -> None:
        await self._call(
            "xadd",
            self._stream_name,
            {"task_id": self._json_dumps(task_id)},
        )

    async def _load_idempotent_task_id(self, idempotency_key: str) -> str | None:
        if not hasattr(self._redis, "get"):
            return None
        raw = await self._call("get", self._idempotency_key(idempotency_key))
        if raw is None:
            return None
        return self._decode(raw)

    async def _reserve_idempotency_key(self, idempotency_key: str, task_id: str) -> bool:
        if not hasattr(self._redis, "set"):
            return True
        result = await self._call(
            "set",
            self._idempotency_key(idempotency_key),
            task_id,
            nx=True,
        )
        return bool(result)

    async def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(self._redis, method_name)(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _task_key(self, task_id: str) -> str:
        return f"{self._stream_name}:task:{task_id}"

    def _idempotency_key(self, idempotency_key: str) -> str:
        return f"{self._stream_name}:idempotency:{idempotency_key}"

    def _normalize_mapping(self, mapping: dict[Any, Any]) -> dict[str, str]:
        return {self._decode(key): self._decode(value) for key, value in mapping.items()}

    def _parse_xautoclaim_messages(
        self,
        result: Any,
    ) -> list[tuple[Any, dict[Any, Any]]]:
        if isinstance(result, tuple) and len(result) >= 2:
            return list(result[1])
        if isinstance(result, list) and len(result) >= 2 and not isinstance(result[0], tuple):
            return list(result[1])
        return []

    def _parse_pending_entries(self, pending: Any) -> list[tuple[str, int]]:
        entries: list[tuple[str, int]] = []
        for entry in pending:
            if isinstance(entry, dict):
                message_id = entry.get("message_id") or entry.get("messageId")
                idle = (
                    entry.get("time_since_delivered")
                    or entry.get("idle")
                    or entry.get("idle_time")
                    or 0
                )
            else:
                message_id = entry[0]
                idle = entry[2] if len(entry) > 2 else 0
            entries.append((self._decode(message_id), int(idle)))
        return entries

    def _decode(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode()
        return str(value)

    def _json_dumps(self, value: Any) -> str:
        return json.dumps(value, separators=(",", ":"))

    def _json_loads(self, value: str) -> Any:
        return json.loads(value)


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError("idempotency_key must not be empty")
    return normalized
