import inspect
import json
from typing import Any

from infra.plugins.tasks.models import TaskEnvelope


class RedisStreamTaskQueue:
    def __init__(
        self,
        redis: Any,
        stream_name: str = "infra:tasks",
        consumer_group: str = "infra",
        consumer_name: str = "tasks",
        pending_min_idle_ms: int = 60_000,
    ) -> None:
        self._redis = redis
        self._stream_name = stream_name
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name
        self._pending_min_idle_ms = pending_min_idle_ms
        self._consumer_group_ready = False
        self._tasks: dict[str, TaskEnvelope] = {}
        self._message_ids: dict[str, str] = {}

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
    ) -> TaskEnvelope:
        task = TaskEnvelope(name=name, payload=payload)
        await self._persist(task)
        await self._call(
            "xadd",
            self._stream_name,
            {"task_id": self._json_dumps(task.id)},
        )
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
                task = await self._load(task_id)
                if task.state not in ("queued", "running"):
                    continue
                running = task.model_copy(update={"state": "running"})
                self._message_ids[task_id] = self._decode(message_id)
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

    async def _persist(self, task: TaskEnvelope) -> None:
        mapping = {
            "id": self._json_dumps(task.id),
            "name": self._json_dumps(task.name),
            "payload": self._json_dumps(task.payload),
            "state": self._json_dumps(task.state),
            "error": self._json_dumps(task.error),
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
            state=self._json_loads(data["state"]),
            error=self._json_loads(data["error"]),
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

    async def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(self._redis, method_name)(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _task_key(self, task_id: str) -> str:
        return f"{self._stream_name}:task:{task_id}"

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
