import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.manager import PluginManager
from infra.plugins.tasks import (
    MemoryTaskQueue,
    RedisStreamTaskQueue,
    TaskState,
    TasksPlugin,
)


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.hashes: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], int] = {}
        self.pending: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.fail_busygroup = False
        self._next_id = 1

    async def hset(self, name: str, mapping: dict[str, str]) -> None:
        self.calls.append(("hset", (name,), {"mapping": mapping}))
        self.hashes[name] = dict(mapping)

    async def hgetall(self, name: str) -> dict[str, str]:
        self.calls.append(("hgetall", (name,), {}))
        return dict(self.hashes[name])

    async def xadd(self, name: str, fields: dict[str, str]) -> str:
        self.calls.append(("xadd", (name,), {"fields": fields}))
        message_id = f"{self._next_id}-0"
        self._next_id += 1
        self.streams.setdefault(name, []).append((message_id, dict(fields)))
        return message_id

    async def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str,
        mkstream: bool,
    ) -> bool:
        self.calls.append(
            (
                "xgroup_create",
                (name, groupname, id),
                {"mkstream": mkstream},
            )
        )
        if self.fail_busygroup or (name, groupname) in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.streams.setdefault(name, [])
        self.groups[(name, groupname)] = 0 if id == "0" else len(self.streams[name])
        return True

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        self.calls.append(
            (
                "xreadgroup",
                (),
                {
                    "groupname": groupname,
                    "consumername": consumername,
                    "streams": streams,
                    "count": count,
                    "block": block,
                },
            )
        )
        stream_name = next(iter(streams))
        if block == 0:
            raise TimeoutError("xreadgroup BLOCK 0 would hang in real Redis")
        group_key = (stream_name, groupname)
        offset = self.groups[group_key]
        messages = self.streams.get(stream_name, [])[offset : offset + count]
        self.groups[group_key] = offset + len(messages)
        pending_entries = self.pending.setdefault(group_key, [])
        for message_id, fields in messages:
            pending_entries.append(
                {
                    "message_id": message_id,
                    "consumer": consumername,
                    "idle": 0,
                    "fields": dict(fields),
                }
            )
        return [(stream_name, messages)] if messages else []

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        count: int,
    ) -> tuple[str, list[tuple[str, dict[str, str]]]]:
        self.calls.append(
            (
                "xautoclaim",
                (name, groupname, consumername, min_idle_time, start_id),
                {"count": count},
            )
        )
        messages: list[tuple[str, dict[str, str]]] = []
        for entry in self.pending.get((name, groupname), []):
            if len(messages) >= count:
                break
            if int(entry["idle"]) >= min_idle_time:
                entry["consumer"] = consumername
                messages.append((str(entry["message_id"]), dict(entry["fields"])))
        return ("0-0", messages)

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        self.calls.append(("xack", (name, groupname, *ids), {}))
        pending = self.pending.get((name, groupname), [])
        self.pending[(name, groupname)] = [
            entry for entry in pending if entry["message_id"] not in ids
        ]
        return len(ids)

    def age_pending(self, stream_name: str, groupname: str, idle: int) -> None:
        for entry in self.pending.get((stream_name, groupname), []):
            entry["idle"] = idle


class FakeDatabaseService:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis

    async def _get_or_create_redis_client(self) -> FakeRedis:
        return self.redis


class FakeDatabasePlugin:
    metadata = PluginMetadata(name="database", version="1.0.0", provides=["database"])
    config_model = None

    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis

    def register(self, ctx: PluginContext) -> None:
        ctx.services["database"] = FakeDatabaseService(self.redis)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("database", HealthState.HEALTHY)


@pytest.mark.asyncio
async def test_memory_task_queue_tracks_complete_and_failed_tasks():
    queue = MemoryTaskQueue()

    first = await queue.enqueue("send_email", {"to": "user@example.com"})
    second = await queue.enqueue("sync_account")

    assert first.state == "queued"
    assert first.payload == {"to": "user@example.com"}
    assert second.payload is None

    dequeued_first = await queue.dequeue()
    assert dequeued_first == first.model_copy(update={"state": "running"})
    assert queue.get(first.id).state == "running"

    await queue.complete(first.id)
    assert queue.get(first.id).state == "completed"
    assert queue.get(first.id).error is None

    dequeued_second = await queue.dequeue()
    assert dequeued_second is not None
    await queue.fail(dequeued_second.id, "network timeout")

    failed = queue.get(second.id)
    assert failed.state == "failed"
    assert failed.error == "network timeout"
    assert await queue.dequeue() is None


@pytest.mark.asyncio
async def test_redis_stream_task_queue_tracks_task_lifecycle_with_json_fields():
    redis = FakeRedis()
    queue = RedisStreamTaskQueue(redis, stream_name="test:tasks", consumer_group="tests")

    task = await queue.enqueue("send_email", {"to": "user@example.com"})

    assert task.state == "queued"
    assert task.payload == {"to": "user@example.com"}
    xadd_call = next(call for call in redis.calls if call[0] == "xadd")
    assert xadd_call[1] == ("test:tasks",)
    assert xadd_call[2]["fields"] == {"task_id": f'"{task.id}"'}

    hset_call = next(call for call in redis.calls if call[0] == "hset")
    stored = hset_call[2]["mapping"]
    assert stored["name"] == '"send_email"'
    assert stored["payload"] == '{"to":"user@example.com"}'
    assert stored["state"] == '"queued"'

    dequeued = await queue.dequeue()
    assert dequeued == task.model_copy(update={"state": "running"})
    assert queue.get(task.id).state == "running"
    xgroup_call = next(call for call in redis.calls if call[0] == "xgroup_create")
    assert xgroup_call[1] == ("test:tasks", "tests", "0")
    assert xgroup_call[2] == {"mkstream": True}

    await queue.complete(task.id)
    completed = queue.get(task.id)
    assert completed.state == "completed"
    assert completed.error is None

    second = await queue.enqueue("sync_account")
    dequeued_second = await queue.dequeue()
    assert dequeued_second is not None

    await queue.fail(second.id, "network timeout")
    failed = queue.get(second.id)
    assert failed.state == "failed"
    assert failed.error == "network timeout"

    assert await queue.dequeue() is None
    assert [call[0] for call in redis.calls].count("xreadgroup") >= 3
    assert [call[0] for call in redis.calls].count("xack") == 2
    assert all(call[2].get("block") is None for call in redis.calls if call[0] == "xreadgroup")


@pytest.mark.asyncio
async def test_redis_stream_task_queue_tolerates_existing_consumer_group():
    redis = FakeRedis()
    await redis.xgroup_create("test:tasks", "tests", "0", mkstream=True)
    queue = RedisStreamTaskQueue(redis, stream_name="test:tasks", consumer_group="tests")

    task = await queue.enqueue("send_email")

    assert await queue.dequeue() == task.model_copy(update={"state": "running"})


@pytest.mark.asyncio
async def test_redis_stream_task_queue_recovers_stale_pending_task():
    redis = FakeRedis()
    first_queue = RedisStreamTaskQueue(
        redis,
        stream_name="test:tasks",
        consumer_group="tests",
        pending_min_idle_ms=5000,
    )
    task = await first_queue.enqueue("send_email")
    assert await first_queue.dequeue() == task.model_copy(update={"state": "running"})
    redis.age_pending("test:tasks", "tests", idle=5000)

    recovery_queue = RedisStreamTaskQueue(
        redis,
        stream_name="test:tasks",
        consumer_group="tests",
        pending_min_idle_ms=5000,
    )

    recovered = await recovery_queue.dequeue()

    assert recovered == task.model_copy(update={"state": "running"})
    assert recovery_queue.get(task.id).state == "running"
    assert [call[0] for call in redis.calls].count("xautoclaim") >= 1


@pytest.mark.asyncio
async def test_redis_stream_task_queue_get_async_loads_persisted_state_for_new_instance():
    redis = FakeRedis()
    first_queue = RedisStreamTaskQueue(redis, stream_name="test:tasks", consumer_group="tests")
    task = await first_queue.enqueue("send_email")

    new_queue = RedisStreamTaskQueue(redis, stream_name="test:tasks", consumer_group="tests")

    with pytest.raises(KeyError):
        new_queue.get(task.id)
    loaded = await new_queue.get_async(task.id)

    assert loaded == task
    assert new_queue.get(task.id) == task


def test_task_state_literal_values_are_public():
    assert TaskState.__args__ == ("queued", "running", "completed", "failed")


@pytest.mark.asyncio
async def test_tasks_plugin_registers_tasks_service():
    settings = InfraSettings(infra={"plugins": {"tasks": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, MemoryTaskQueue)

    task = await service.enqueue("index_document", {"id": "doc-1"})
    assert task.name == "index_document"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_tasks_plugin_memory_adapter_can_use_configured_service_name():
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {"adapter": "memory", "service": "jobs"},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])

    await manager.startup()

    assert isinstance(manager.get("jobs"), MemoryTaskQueue)
    assert manager.get("tasks") is None

    await manager.shutdown()


@pytest.mark.asyncio
async def test_tasks_plugin_redis_adapter_accepts_injected_client_for_tests():
    redis = FakeRedis()
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {
                        "adapter": "redis",
                        "redis": redis,
                        "consumer_name": "worker-a",
                        "pending_min_idle_ms": 1234,
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, RedisStreamTaskQueue)
    assert service._consumer_name == "worker-a"
    assert service._pending_min_idle_ms == 1234
    task = await service.enqueue("index_document", {"id": "doc-1"})
    assert queue_task_name(redis, task.id) == "index_document"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_tasks_plugin_redis_adapter_uses_database_service_client():
    redis = FakeRedis()
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {"enabled": True},
                "tasks": {
                    "enabled": True,
                    "config": {
                        "adapter": "redis",
                        "consumer_name": "worker-b",
                        "pending_min_idle_ms": 5678,
                    },
                },
            }
        }
    )
    manager = PluginManager(
        settings=settings,
        plugins=[FakeDatabasePlugin(redis), TasksPlugin()],
    )

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, RedisStreamTaskQueue)
    assert service._consumer_name == "worker-b"
    assert service._pending_min_idle_ms == 5678
    task = await service.enqueue("index_document", {"id": "doc-1"})
    assert queue_task_name(redis, task.id) == "index_document"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_tasks_plugin_forced_redis_adapter_requires_redis_backing():
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {"enabled": True, "config": {"adapter": "redis"}},
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])

    with pytest.raises(RuntimeError, match="Redis task adapter requires"):
        await manager.startup()


def queue_task_name(redis: FakeRedis, task_id: str) -> str:
    return redis.hashes[f"infra:tasks:task:{task_id}"]["name"].strip('"')
