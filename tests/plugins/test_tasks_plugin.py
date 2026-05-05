import pytest

from infra.config.models import InfraSettings
from infra.plugins.manager import PluginManager
from infra.plugins.tasks import MemoryTaskQueue, TaskState, TasksPlugin


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
