from typing import cast

import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.manager import PluginDependencyError, PluginManager
from infra.plugins.tasks import (
    CeleryTaskQueue,
    KafkaTaskQueue,
    MemoryTaskQueue,
    RedisStreamTaskQueue,
    SqsTaskQueue,
    TaskQueueBackendRegistry,
    TasksPlugin,
    TaskState,
)
from infra.plugins.tasks.adapters._broker import BrokerMessage
from infra.plugins.tasks.models import TaskEnvelope


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], int] = {}
        self.pending: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.fail_busygroup = False
        self.ping_ok = True
        self._next_id = 1

    async def ping(self) -> bool:
        self.calls.append(("ping", (), {}))
        return self.ping_ok

    async def hset(self, name: str, mapping: dict[str, str]) -> None:
        self.calls.append(("hset", (name,), {"mapping": mapping}))
        self.hashes[name] = dict(mapping)

    async def hgetall(self, name: str) -> dict[str, str]:
        self.calls.append(("hgetall", (name,), {}))
        return dict(self.hashes[name])

    async def get(self, name: str) -> str | None:
        self.calls.append(("get", (name,), {}))
        return self.values.get(name)

    async def set(self, name: str, value: str, nx: bool = False) -> bool:
        self.calls.append(("set", (name, value), {"nx": nx}))
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

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
            idle = cast(int, entry["idle"])
            fields = cast(dict[str, str], entry["fields"])
            if idle >= min_idle_time:
                entry["consumer"] = consumername
                messages.append((str(entry["message_id"]), dict(fields)))
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

    async def get_redis_client(self) -> FakeRedis:
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


class FakeTaskEntryPoint:
    def __init__(self, name: str, loaded: object) -> None:
        self.name = name
        self.loaded = loaded

    def load(self) -> object:
        return self.loaded


class CustomTaskQueue:
    name = "custom"

    def __init__(self, config):
        self.config = dict(config)
        self.tasks = {}

    async def enqueue(
        self,
        name,
        payload=None,
        *,
        idempotency_key=None,
        delay_seconds=0,
        max_attempts=1,
    ):
        from infra.plugins.tasks.models import TaskEnvelope

        task = TaskEnvelope(
            name=name,
            payload=payload,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
        )
        self.tasks[task.id] = task
        return task

    async def dequeue(self):
        return None

    async def complete(self, task_id):
        return None

    async def fail(self, task_id, reason):
        return None

    async def retry(self, task_id, reason, *, delay_seconds=0):
        return None

    async def dead_letter(self, task_id, reason):
        return None

    def get(self, task_id):
        return self.tasks[task_id]


class FakeSqsClient:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.sent: list[dict[str, object]] = []
        self.deleted: list[str] = []
        self.visibility_changes: list[dict[str, object]] = []
        self.attributes_checked = False
        self._next_receipt = 1

    def send_message(self, **kwargs):
        receipt = f"receipt-{self._next_receipt}"
        self._next_receipt += 1
        self.sent.append(dict(kwargs))
        self.messages.append({"Body": str(kwargs["MessageBody"]), "ReceiptHandle": receipt})
        return {"MessageId": receipt}

    def receive_message(self, **kwargs):
        if not self.messages:
            return {}
        return {"Messages": [self.messages.pop(0)]}

    def delete_message(self, **kwargs):
        self.deleted.append(str(kwargs["ReceiptHandle"]))
        return {}

    def change_message_visibility(self, **kwargs):
        self.visibility_changes.append(dict(kwargs))
        return {}

    def get_queue_attributes(self, **kwargs):
        self.attributes_checked = True
        return {"Attributes": {"QueueArn": "arn:aws:sqs:us-east-1:123:tasks"}}


class FakeKafkaMessage:
    def __init__(self, value: bytes) -> None:
        self.value = value


class FakeKafkaProducer:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes, bytes | None]] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_and_wait(self, topic: str, value: bytes, key: bytes | None = None) -> None:
        self.messages.append((topic, value, key))


class FakeKafkaConsumer:
    def __init__(self, producer: FakeKafkaProducer) -> None:
        self.producer = producer
        self.started = False
        self.stopped = False
        self.commits = 0
        self._offset = 0

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def getone(self) -> FakeKafkaMessage:
        if self._offset >= len(self.producer.messages):
            raise TimeoutError
        _topic, value, _key = self.producer.messages[self._offset]
        self._offset += 1
        return FakeKafkaMessage(value)

    async def commit(self) -> None:
        self.commits += 1


class FakeCeleryReceipt:
    def __init__(self) -> None:
        self.acked = False

    def ack(self) -> None:
        self.acked = True


class FakeCeleryTransport:
    def __init__(self) -> None:
        self.messages: list[BrokerMessage] = []
        self.published: list[TaskEnvelope] = []
        self.dead_letters: list[tuple[str, TaskEnvelope]] = []
        self.health_checked = False
        self.closed = False

    async def publish(self, task: TaskEnvelope) -> None:
        receipt = FakeCeleryReceipt()
        self.published.append(task)
        self.messages.append(BrokerMessage(task=task, receipt=receipt))

    async def publish_dead_letter(self, task: TaskEnvelope, queue_name: str) -> None:
        self.dead_letters.append((queue_name, task))

    async def receive(self) -> BrokerMessage | None:
        if not self.messages:
            return None
        return self.messages.pop(0)

    async def ack(self, receipt: FakeCeleryReceipt) -> None:
        receipt.ack()

    async def health_check(self) -> bool:
        self.health_checked = True
        return True

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_memory_task_queue_tracks_complete_and_failed_tasks():
    queue = MemoryTaskQueue()

    first = await queue.enqueue("send_email", {"to": "user@example.com"})
    second = await queue.enqueue("sync_account")

    assert first.state == "queued"
    assert first.payload == {"to": "user@example.com"}
    assert second.payload is None

    dequeued_first = await queue.dequeue()
    assert dequeued_first == first.model_copy(update={"state": "running", "attempts": 1})
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
async def test_memory_task_queue_retries_after_backoff_and_dead_letters():
    now = 1000.0
    queue = MemoryTaskQueue(now=lambda: now)
    task = await queue.enqueue("sync_account", max_attempts=2)

    first = await queue.dequeue()
    assert first == task.model_copy(update={"state": "running", "attempts": 1})

    await queue.retry(task.id, "temporary outage", delay_seconds=30)
    queued = queue.get(task.id)
    assert queued.state == "queued"
    assert queued.error == "temporary outage"
    assert queued.available_at == 1030.0
    assert await queue.dequeue() is None

    now = 1030.0
    second = await queue.dequeue()
    assert second is not None
    assert second.attempts == 2

    await queue.dead_letter(task.id, "permanent outage")
    dead = queue.get(task.id)
    assert dead.state == "dead_lettered"
    assert dead.error == "permanent outage"


@pytest.mark.asyncio
async def test_memory_task_queue_deduplicates_by_idempotency_key_and_delays_initial_delivery():
    now = 1000.0
    queue = MemoryTaskQueue(now=lambda: now)

    first = await queue.enqueue(
        "sync_account",
        {"account_id": "a"},
        idempotency_key=" account:a ",
        delay_seconds=15,
    )
    duplicate = await queue.enqueue(
        "sync_account",
        {"account_id": "a"},
        idempotency_key="account:a",
    )

    assert duplicate == first
    assert first.idempotency_key == "account:a"
    assert first.available_at == 1015.0
    assert await queue.dequeue() is None

    now = 1015.0
    dequeued = await queue.dequeue()
    assert dequeued == first.model_copy(update={"state": "running", "attempts": 1})
    assert await queue.dequeue() is None


@pytest.mark.asyncio
async def test_memory_task_queue_rejects_blank_idempotency_key():
    queue = MemoryTaskQueue()

    with pytest.raises(ValueError, match="idempotency_key"):
        await queue.enqueue("sync_account", idempotency_key=" ")


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
    assert stored["idempotency_key"] == "null"
    assert stored["state"] == '"queued"'
    assert stored["attempts"] == "0"
    assert stored["max_attempts"] == "1"

    dequeued = await queue.dequeue()
    assert dequeued == task.model_copy(update={"state": "running", "attempts": 1})
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

    assert await queue.dequeue() == task.model_copy(update={"state": "running", "attempts": 1})


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
    assert await first_queue.dequeue() == task.model_copy(
        update={"state": "running", "attempts": 1}
    )
    redis.age_pending("test:tasks", "tests", idle=5000)

    recovery_queue = RedisStreamTaskQueue(
        redis,
        stream_name="test:tasks",
        consumer_group="tests",
        pending_min_idle_ms=5000,
    )

    recovered = await recovery_queue.dequeue()

    assert recovered == task.model_copy(update={"state": "running", "attempts": 2})
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


@pytest.mark.asyncio
async def test_redis_stream_task_queue_retries_with_delayed_delivery():
    now = 1000.0
    redis = FakeRedis()
    queue = RedisStreamTaskQueue(
        redis,
        stream_name="test:tasks",
        consumer_group="tests",
        now=lambda: now,
    )
    task = await queue.enqueue("sync_account", max_attempts=2)

    first = await queue.dequeue()
    assert first == task.model_copy(update={"state": "running", "attempts": 1})

    await queue.retry(task.id, "temporary outage", delay_seconds=30)
    retried = queue.get(task.id)
    assert retried.state == "queued"
    assert retried.error == "temporary outage"
    assert retried.available_at == 1030.0

    assert await queue.dequeue() is None

    now = 1030.0
    second = await queue.dequeue()
    assert second is not None
    assert second.state == "running"
    assert second.attempts == 2

    await queue.dead_letter(task.id, "permanent outage")
    dead = queue.get(task.id)
    assert dead.state == "dead_lettered"
    assert dead.error == "permanent outage"


@pytest.mark.asyncio
async def test_redis_stream_task_queue_deduplicates_by_idempotency_key_and_delays_initial_delivery():
    now = 1000.0
    redis = FakeRedis()
    queue = RedisStreamTaskQueue(
        redis,
        stream_name="test:tasks",
        consumer_group="tests",
        now=lambda: now,
    )

    first = await queue.enqueue(
        "sync_account",
        {"account_id": "a"},
        idempotency_key=" account:a ",
        delay_seconds=15,
    )
    duplicate = await queue.enqueue(
        "sync_account",
        {"account_id": "a"},
        idempotency_key="account:a",
    )

    assert duplicate == first
    assert first.idempotency_key == "account:a"
    assert first.available_at == 1015.0
    assert redis.values["test:tasks:idempotency:account:a"] == first.id
    assert [call[0] for call in redis.calls].count("xadd") == 1
    assert await queue.dequeue() is None

    now = 1015.0
    dequeued = await queue.dequeue()
    assert dequeued == first.model_copy(update={"state": "running", "attempts": 1})


@pytest.mark.asyncio
async def test_redis_stream_task_queue_rejects_blank_idempotency_key():
    redis = FakeRedis()
    queue = RedisStreamTaskQueue(redis)

    with pytest.raises(ValueError, match="idempotency_key"):
        await queue.enqueue("sync_account", idempotency_key=" ")


def test_task_state_literal_values_are_public():
    assert TaskState.__args__ == (
        "queued",
        "running",
        "completed",
        "failed",
        "dead_lettered",
    )


@pytest.mark.asyncio
async def test_tasks_plugin_registers_tasks_service():
    settings = InfraSettings(infra={"plugins": {"tasks": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, TaskQueueBackendRegistry)
    assert isinstance(service.provider(), MemoryTaskQueue)

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
                    "config": {"default_provider": "memory", "service": "jobs"},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])

    await manager.startup()

    service = manager.get("jobs")
    assert isinstance(service, TaskQueueBackendRegistry)
    assert isinstance(service.provider(), MemoryTaskQueue)
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
                        "default_provider": "redis",
                        "providers": {
                            "redis": {
                                "consumer_name": "worker-a",
                                "pending_min_idle_ms": 1234,
                            }
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin(redis=redis)])

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, TaskQueueBackendRegistry)
    queue = service.provider()
    assert isinstance(queue, RedisStreamTaskQueue)
    assert queue._consumer_name == "worker-a"
    assert queue._pending_min_idle_ms == 1234
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
                        "default_provider": "redis",
                        "providers": {
                            "redis": {
                                "consumer_name": "worker-b",
                                "pending_min_idle_ms": 5678,
                            }
                        },
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
    assert isinstance(service, TaskQueueBackendRegistry)
    queue = service.provider()
    assert isinstance(queue, RedisStreamTaskQueue)
    assert queue._consumer_name == "worker-b"
    assert queue._pending_min_idle_ms == 5678
    task = await service.enqueue("index_document", {"id": "doc-1"})
    assert queue_task_name(redis, task.id) == "index_document"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_tasks_plugin_registers_builtin_sqs_backend():
    sqs = FakeSqsClient()
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {
                        "default_provider": "sqs",
                        "providers": {
                            "sqs": {
                                "queue_url": "https://sqs.us-east-1.amazonaws.com/123/tasks",
                                "wait_time_seconds": 1,
                                "visibility_timeout": 30,
                            }
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin(sqs_client=sqs)])

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, TaskQueueBackendRegistry)
    queue = service.provider()
    assert isinstance(queue, SqsTaskQueue)
    assert sqs.attributes_checked is True
    task = await service.enqueue("index_document", {"id": "doc-1"})

    dequeued = await service.dequeue()
    assert dequeued == task.model_copy(update={"state": "running", "attempts": 1})
    await service.complete(task.id)

    assert queue.get(task.id).state == "completed"
    assert sqs.deleted == ["receipt-1"]
    assert sqs.sent[0]["QueueUrl"] == "https://sqs.us-east-1.amazonaws.com/123/tasks"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_tasks_plugin_registers_builtin_kafka_backend():
    producer = FakeKafkaProducer()
    consumer = FakeKafkaConsumer(producer)
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {
                        "default_provider": "kafka",
                        "providers": {
                            "kafka": {
                                "bootstrap_servers": "localhost:9092",
                                "topic": "tasks",
                                "group_id": "workers",
                                "dead_letter_topic": "tasks.dead",
                            }
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(
        settings=settings,
        plugins=[TasksPlugin(kafka_producer=producer, kafka_consumer=consumer)],
    )

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, TaskQueueBackendRegistry)
    queue = service.provider()
    assert isinstance(queue, KafkaTaskQueue)
    assert producer.started is True
    assert consumer.started is True
    task = await service.enqueue("index_document", {"id": "doc-1"})

    dequeued = await service.dequeue()
    assert dequeued == task.model_copy(update={"state": "running", "attempts": 1})
    await service.dead_letter(task.id, "no handler")

    assert queue.get(task.id).state == "dead_lettered"
    assert consumer.commits == 1
    assert producer.messages[-1][0] == "tasks.dead"

    await manager.shutdown()
    assert producer.stopped is True
    assert consumer.stopped is True


@pytest.mark.asyncio
async def test_tasks_plugin_registers_builtin_celery_backend():
    transport = FakeCeleryTransport()
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {
                        "default_provider": "celery",
                        "providers": {
                            "celery": {
                                "broker_url": "redis://localhost:6379/0",
                                "queue_name": "infra.tasks",
                                "dead_letter_queue_name": "infra.tasks.dead",
                            }
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(
        settings=settings,
        plugins=[TasksPlugin(celery_transport=transport)],
    )

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, TaskQueueBackendRegistry)
    queue = service.provider()
    assert isinstance(queue, CeleryTaskQueue)
    assert transport.health_checked is True
    task = await service.enqueue("index_document", {"id": "doc-1"})

    dequeued = await service.dequeue()
    assert dequeued == task.model_copy(update={"state": "running", "attempts": 1})
    await service.retry(task.id, "temporary outage", delay_seconds=0)

    assert queue.get(task.id).state == "queued"
    assert len(transport.published) == 2

    retry_task = await service.dequeue()
    assert retry_task is not None
    await service.dead_letter(task.id, "permanent outage")

    assert transport.dead_letters[0][0] == "infra.tasks.dead"
    assert queue.get(task.id).state == "dead_lettered"

    await manager.shutdown()
    assert transport.closed is True


@pytest.mark.asyncio
async def test_tasks_plugin_loads_external_provider_from_entry_point(monkeypatch):
    def provider_factory(config):
        return CustomTaskQueue(config)

    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeTaskEntryPoint("custom", provider_factory)],
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {"queue_url": "memory://custom"}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])

    await manager.startup()

    service = manager.get("tasks")
    assert isinstance(service, TaskQueueBackendRegistry)
    provider = service.provider()
    assert isinstance(provider, CustomTaskQueue)
    assert provider.config == {"queue_url": "memory://custom"}
    task = await service.enqueue("index_document", {"id": "doc-1"})
    assert provider.get(task.id).name == "index_document"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_tasks_plugin_forced_redis_adapter_requires_redis_backing():
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {"enabled": True, "config": {"default_provider": "redis"}},
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])

    with pytest.raises(RuntimeError, match="Redis task provider requires"):
        await manager.startup()


@pytest.mark.asyncio
async def test_tasks_plugin_reports_redis_health_details():
    redis = FakeRedis()
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {"default_provider": "redis", "service": "jobs"},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin(redis=redis)])

    await manager.startup()

    status = manager.health.snapshot()["tasks"]
    assert status.status is HealthState.HEALTHY
    assert status.details == {"provider": "redis", "service": "jobs"}
    assert ("ping", (), {}) in redis.calls

    await manager.shutdown()


@pytest.mark.asyncio
async def test_tasks_plugin_reports_unhealthy_when_redis_health_check_fails():
    redis = FakeRedis()
    redis.ping_ok = False
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {"default_provider": "redis"},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin(redis=redis)])

    with pytest.raises(PluginDependencyError, match="plugin is unhealthy: tasks"):
        await manager.startup()

    status = manager.health.snapshot()["tasks"]
    assert status.status is HealthState.UNHEALTHY
    assert status.message == "task queue health check failed"
    assert status.details == {"provider": "redis", "service": "tasks"}


def queue_task_name(redis: FakeRedis, task_id: str) -> str:
    return redis.hashes[f"infra:tasks:task:{task_id}"]["name"].strip('"')
