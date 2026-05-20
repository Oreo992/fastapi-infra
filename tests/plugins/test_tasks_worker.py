import asyncio
from contextlib import contextmanager
from typing import Any

import pytest

from infra.plugins.tasks import (
    MemoryTaskQueue,
    TaskEnvelope,
    TaskWorker,
    TaskWorkerRunConfig,
    run_task_worker,
)


def payload_for(task: TaskEnvelope) -> dict[str, Any]:
    assert task.payload is not None
    return task.payload


class FakeInstrumentation:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.timings: dict[str, list[float]] = {}
        self.spans: list[tuple[str, dict[str, str | int | float | bool] | None]] = []

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def timing(self, name: str, value: float) -> None:
        self.timings.setdefault(name, []).append(value)

    @contextmanager
    def span(self, name: str, attributes: dict[str, str | int | float | bool] | None = None):
        self.spans.append((name, attributes))
        yield


@pytest.mark.asyncio
async def test_task_worker_run_once_completes_registered_handler():
    queue = MemoryTaskQueue()
    worker = TaskWorker(queue)
    handled: list[TaskEnvelope] = []

    async def send_email(task: TaskEnvelope) -> None:
        handled.append(task)

    worker.handler("send_email", send_email)
    task = await queue.enqueue("send_email", {"to": "user@example.com"})

    processed = await worker.run_once()

    assert processed is True
    assert [item.payload for item in handled] == [{"to": "user@example.com"}]
    assert queue.get(task.id).state == "completed"
    assert queue.get(task.id).error is None


@pytest.mark.asyncio
async def test_task_worker_records_instrumentation_for_completed_task() -> None:
    queue = MemoryTaskQueue()
    instrumentation = FakeInstrumentation()
    worker = TaskWorker(queue, instrumentation=instrumentation)

    @worker.handler("send_email")
    async def send_email(task: TaskEnvelope) -> None:
        return None

    task = await queue.enqueue("send_email", {"to": "user@example.com"})

    assert await worker.run_once() is True

    assert instrumentation.counters["task_worker_tasks_total"] == 1
    assert instrumentation.counters["task_worker_completed_total"] == 1
    assert instrumentation.counters["task_worker_task_send_email_completed_total"] == 1
    assert "task_worker_task_seconds" in instrumentation.timings
    assert instrumentation.spans == [
        (
            "task.worker.run",
            {
                "task.name": "send_email",
                "task.id": task.id,
                "task.attempt": 1,
            },
        )
    ]


@pytest.mark.asyncio
async def test_task_worker_handler_can_be_registered_as_decorator():
    queue = MemoryTaskQueue()
    worker = TaskWorker(queue)
    handled: list[str] = []

    @worker.handler("send_email")
    async def send_email(task: TaskEnvelope) -> None:
        handled.append(str(payload_for(task)["to"]))

    task = await queue.enqueue("send_email", {"to": "user@example.com"})

    assert await worker.run_once() is True
    assert handled == ["user@example.com"]
    assert queue.get(task.id).state == "completed"


@pytest.mark.asyncio
async def test_task_worker_run_once_returns_false_when_queue_is_idle():
    worker = TaskWorker(MemoryTaskQueue())

    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_task_worker_marks_handler_exceptions_failed():
    queue = MemoryTaskQueue()
    worker = TaskWorker(queue)

    async def sync_account(task: TaskEnvelope) -> None:
        raise RuntimeError(f"sync failed for {payload_for(task)['id']}")

    worker.handler("sync_account", sync_account)
    task = await queue.enqueue("sync_account", {"id": "acct-1"})

    assert await worker.run_once() is True

    failed = queue.get(task.id)
    assert failed.state == "dead_lettered"
    assert failed.error == "sync failed for acct-1"


@pytest.mark.asyncio
async def test_task_worker_marks_unknown_handlers_failed():
    queue = MemoryTaskQueue()
    worker = TaskWorker(queue)
    task = await queue.enqueue("missing_handler")

    assert await worker.run_once() is True

    failed = queue.get(task.id)
    assert failed.state == "dead_lettered"
    assert failed.error == "No task handler registered for 'missing_handler'"


@pytest.mark.asyncio
async def test_task_worker_retries_failed_handler_before_dead_lettering():
    now = 1000.0
    queue = MemoryTaskQueue(now=lambda: now)
    worker = TaskWorker(queue, retry_backoff=lambda task: task.attempts * 2)
    attempts = 0

    async def sync_account(task: TaskEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary outage")

    worker.handler("sync_account", sync_account)
    task = await queue.enqueue("sync_account", {"id": "acct-1"}, max_attempts=2)

    assert await worker.run_once() is True
    retried = queue.get(task.id)
    assert retried.state == "queued"
    assert retried.error == "temporary outage"
    assert retried.attempts == 1
    assert retried.available_at == 1002.0

    now = 1002.0
    assert await worker.run_once() is True

    completed = queue.get(task.id)
    assert completed.state == "completed"
    assert completed.error is None
    assert completed.attempts == 2


@pytest.mark.asyncio
async def test_task_worker_run_until_stopped_polls_and_stops_cleanly():
    queue = MemoryTaskQueue()
    worker = TaskWorker(queue)
    handled = asyncio.Event()

    async def index_document(task: TaskEnvelope) -> None:
        handled.set()

    worker.handler("index_document", index_document)
    task = await queue.enqueue("index_document", {"id": "doc-1"})

    loop_task = asyncio.create_task(worker.run_until_stopped(poll_interval=0, idle_sleep=0.01))
    await asyncio.wait_for(handled.wait(), timeout=1)
    worker.stop()
    await asyncio.wait_for(loop_task, timeout=1)

    assert queue.get(task.id).state == "completed"


@pytest.mark.asyncio
async def test_task_worker_run_stops_after_max_tasks_and_returns_stats():
    queue = MemoryTaskQueue()
    worker = TaskWorker(queue)
    handled: list[str] = []

    @worker.handler("index_document")
    async def index_document(task: TaskEnvelope) -> None:
        handled.append(str(payload_for(task)["id"]))

    first = await queue.enqueue("index_document", {"id": "doc-1"})
    second = await queue.enqueue("index_document", {"id": "doc-2"})

    stats = await worker.run(
        TaskWorkerRunConfig(
            poll_interval=0,
            idle_sleep=0,
            max_tasks=1,
        )
    )

    assert stats.processed == 1
    assert stats.idle_polls == 0
    assert stats.stopped is False
    assert stats.completed == 1
    assert stats.retried == 0
    assert stats.dead_lettered == 0
    assert handled == ["doc-1"]
    assert queue.get(first.id).state == "completed"
    assert queue.get(second.id).state == "queued"


@pytest.mark.asyncio
async def test_task_worker_run_processes_tasks_concurrently() -> None:
    queue = MemoryTaskQueue()
    worker = TaskWorker(queue)
    running = 0
    max_running = 0
    handled: list[str] = []

    @worker.handler("index_document")
    async def index_document(task: TaskEnvelope) -> None:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.01)
        handled.append(str(payload_for(task)["id"]))
        running -= 1

    first = await queue.enqueue("index_document", {"id": "doc-1"})
    second = await queue.enqueue("index_document", {"id": "doc-2"})
    third = await queue.enqueue("index_document", {"id": "doc-3"})

    stats = await worker.run(
        TaskWorkerRunConfig(
            poll_interval=0,
            idle_sleep=0,
            max_tasks=2,
            concurrency=2,
        )
    )

    assert stats.processed == 2
    assert stats.completed == 2
    assert stats.retried == 0
    assert stats.dead_lettered == 0
    assert max_running == 2
    assert sorted(handled) == ["doc-1", "doc-2"]
    assert queue.get(first.id).state == "completed"
    assert queue.get(second.id).state == "completed"
    assert queue.get(third.id).state == "queued"


@pytest.mark.asyncio
async def test_task_worker_run_stats_count_retries_and_dead_letters() -> None:
    queue = MemoryTaskQueue()
    worker = TaskWorker(queue, retry_backoff=0)

    @worker.handler("sync_account")
    async def sync_account(task: TaskEnvelope) -> None:
        raise RuntimeError("temporary outage")

    retry_task = await queue.enqueue("sync_account", max_attempts=2)
    dead_task = await queue.enqueue("sync_account", max_attempts=1)
    unknown_task = await queue.enqueue("missing_handler")

    stats = await worker.run(
        TaskWorkerRunConfig(
            poll_interval=0,
            idle_sleep=0,
            max_tasks=3,
        )
    )

    assert stats.processed == 3
    assert stats.completed == 0
    assert stats.retried == 1
    assert stats.dead_lettered == 2
    assert queue.get(retry_task.id).state == "queued"
    assert queue.get(dead_task.id).state == "dead_lettered"
    assert queue.get(unknown_task.id).state == "dead_lettered"


@pytest.mark.asyncio
async def test_task_worker_records_retry_dead_letter_and_idle_metrics() -> None:
    queue = MemoryTaskQueue()
    instrumentation = FakeInstrumentation()
    worker = TaskWorker(queue, retry_backoff=0, instrumentation=instrumentation)

    @worker.handler("sync.account")
    async def sync_account(task: TaskEnvelope) -> None:
        raise RuntimeError("temporary outage")

    await queue.enqueue("sync.account", max_attempts=2)
    await queue.enqueue("missing_handler")

    stats = await worker.run(
        TaskWorkerRunConfig(
            poll_interval=0,
            idle_sleep=0,
            max_tasks=2,
            idle_poll_limit=1,
        )
    )

    assert stats.retried == 1
    assert stats.dead_lettered == 1
    assert instrumentation.counters["task_worker_retried_total"] == 1
    assert instrumentation.counters["task_worker_dead_lettered_total"] == 1
    assert instrumentation.counters["task_worker_task_sync_account_retried_total"] == 1
    assert instrumentation.counters["task_worker_task_missing_handler_dead_lettered_total"] == 1
    assert "task_worker_idle_polls_total" not in instrumentation.counters


@pytest.mark.asyncio
async def test_task_worker_run_can_stop_after_idle_poll_limit():
    worker = TaskWorker(MemoryTaskQueue())

    @worker.handler("index_document")
    async def index_document(task: TaskEnvelope) -> None:
        raise AssertionError("handler should not run")

    stats = await worker.run(
        TaskWorkerRunConfig(
            idle_sleep=0,
            idle_poll_limit=2,
        )
    )

    assert stats.processed == 0
    assert stats.idle_polls == 2
    assert stats.stopped is False
    assert stats.completed == 0


@pytest.mark.asyncio
async def test_task_worker_records_idle_poll_metric() -> None:
    instrumentation = FakeInstrumentation()
    worker = TaskWorker(MemoryTaskQueue(), instrumentation=instrumentation)

    @worker.handler("index_document")
    async def index_document(task: TaskEnvelope) -> None:
        raise AssertionError("handler should not run")

    await worker.run(TaskWorkerRunConfig(idle_sleep=0, idle_poll_limit=2))

    assert instrumentation.counters["task_worker_idle_polls_total"] == 2


@pytest.mark.asyncio
async def test_run_task_worker_requires_registered_handlers_by_default():
    worker = TaskWorker(MemoryTaskQueue())

    with pytest.raises(RuntimeError, match="registered handler"):
        await run_task_worker(worker, install_signal_handlers=False)


@pytest.mark.asyncio
async def test_run_task_worker_returns_stopped_stats_when_worker_is_stopped():
    queue = MemoryTaskQueue()
    worker = TaskWorker(queue)

    @worker.handler("index_document")
    async def index_document(task: TaskEnvelope) -> None:
        worker.stop()

    await queue.enqueue("index_document", {"id": "doc-1"})

    stats = await run_task_worker(
        worker,
        TaskWorkerRunConfig(idle_sleep=0),
        install_signal_handlers=False,
    )

    assert stats.processed == 1
    assert stats.stopped is True


def test_task_worker_run_config_validates_bounds():
    with pytest.raises(ValueError, match="poll_interval"):
        TaskWorkerRunConfig(poll_interval=-1)
    with pytest.raises(ValueError, match="max_tasks"):
        TaskWorkerRunConfig(max_tasks=0)
    with pytest.raises(ValueError, match="concurrency"):
        TaskWorkerRunConfig(concurrency=0)
