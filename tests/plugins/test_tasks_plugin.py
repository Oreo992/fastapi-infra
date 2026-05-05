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

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int,
        block: int,
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
        messages = self.streams.get(stream_name, [])
        return [(stream_name, [messages.pop(0)])] if messages else []

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        self.calls.append(("xack", (name, groupname, *ids), {}))
        return len(ids)


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
                    "config": {"adapter": "redis", "redis": redis},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, RedisStreamTaskQueue)
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
                "tasks": {"enabled": True, "config": {"adapter": "redis"}},
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
