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
    ) -> None:
        self._redis = redis
        self._stream_name = stream_name
        self._consumer_group = consumer_group
        self._consumer_name = "tasks"
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
        messages = await self._call(
            "xreadgroup",
            groupname=self._consumer_group,
            consumername=self._consumer_name,
            streams={self._stream_name: ">"},
            count=1,
            block=0,
        )
        if not messages:
            return None

        for _stream, stream_messages in messages:
            for message_id, fields in stream_messages:
                normalized = self._normalize_mapping(fields)
                task_id = self._json_loads(normalized["task_id"])
                task = await self._load(task_id)
                if task.state != "queued":
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

    def get(self, task_id: str) -> TaskEnvelope:
        return self._tasks[task_id].model_copy(deep=True)

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

    def _decode(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode()
        return str(value)

    def _json_dumps(self, value: Any) -> str:
        return json.dumps(value, separators=(",", ":"))

    def _json_loads(self, value: str) -> Any:
        return json.loads(value)
