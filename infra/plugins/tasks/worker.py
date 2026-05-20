import asyncio
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, ContextManager, Literal, Protocol, overload

from infra.plugins.tasks.models import TaskEnvelope
from infra.plugins.tasks.queue import TaskQueue

TaskHandler = Callable[[TaskEnvelope], Awaitable[Any]]
RetryBackoff = float | Callable[[TaskEnvelope], float]
TaskRunOutcome = Literal["idle", "completed", "retried", "dead_lettered"]


@dataclass(frozen=True)
class TaskWorkerRunConfig:
    poll_interval: float = 0
    idle_sleep: float = 1
    max_tasks: int | None = None
    idle_poll_limit: int | None = None
    require_handlers: bool = True
    concurrency: int = 1

    def __post_init__(self) -> None:
        if self.poll_interval < 0:
            raise ValueError("poll_interval must be greater than or equal to 0")
        if self.idle_sleep < 0:
            raise ValueError("idle_sleep must be greater than or equal to 0")
        if self.max_tasks is not None and self.max_tasks <= 0:
            raise ValueError("max_tasks must be greater than 0")
        if self.idle_poll_limit is not None and self.idle_poll_limit <= 0:
            raise ValueError("idle_poll_limit must be greater than 0")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be greater than 0")


@dataclass(frozen=True)
class TaskWorkerRunStats:
    processed: int
    idle_polls: int
    stopped: bool
    completed: int = 0
    retried: int = 0
    dead_lettered: int = 0


@dataclass(frozen=True)
class _TaskRunResult:
    outcome: TaskRunOutcome

    @property
    def processed(self) -> bool:
        return self.outcome != "idle"


class TaskInstrumentation(Protocol):
    def increment(self, name: str, amount: int = 1) -> None: ...

    def timing(self, name: str, value: float) -> None: ...

    def span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> ContextManager[Any]: ...


class TaskWorker:
    def __init__(
        self,
        queue: TaskQueue,
        *,
        retry_backoff: RetryBackoff = 0,
        instrumentation: TaskInstrumentation | None = None,
    ) -> None:
        self._queue = queue
        self._handlers: dict[str, TaskHandler] = {}
        self._retry_backoff = retry_backoff
        self._instrumentation = instrumentation
        self._stopped = False
        self._stop_event: asyncio.Event | None = None

    @property
    def registered_handlers(self) -> frozenset[str]:
        return frozenset(self._handlers)

    @overload
    def handler(self, name: str) -> Callable[[TaskHandler], TaskHandler]: ...

    @overload
    def handler(self, name: str, handler: TaskHandler) -> TaskHandler: ...

    def handler(
        self,
        name: str,
        handler: TaskHandler | None = None,
    ) -> TaskHandler | Callable[[TaskHandler], TaskHandler]:
        if not name:
            raise ValueError("Task handler name must not be empty")

        def register(candidate: TaskHandler) -> TaskHandler:
            if not callable(candidate):
                raise TypeError("Task handler must be callable")
            self._handlers[name] = candidate
            return candidate

        if handler is None:
            return register
        return register(handler)

    async def run_once(self) -> bool:
        return (await self._run_once()).processed

    async def _run_once(self) -> _TaskRunResult:
        task = await self._queue.dequeue()
        if task is None:
            return _TaskRunResult("idle")

        start_time = time.monotonic()
        with self._span(
            "task.worker.run",
            {
                "task.name": task.name,
                "task.id": task.id,
                "task.attempt": task.attempts,
            },
        ):
            result = await self._process_task(task)
        self._record_task_result(task, result, start_time)
        return result

    async def _process_task(self, task: TaskEnvelope) -> _TaskRunResult:
        handler = self._handlers.get(task.name)
        if handler is None:
            await self._queue.dead_letter(
                task.id,
                f"No task handler registered for {task.name!r}",
            )
            return _TaskRunResult("dead_lettered")

        try:
            result = handler(task)
            if not isawaitable(result):
                raise TypeError(f"Task handler {task.name!r} must be async")
            await result
        except Exception as exc:
            return await self._handle_failure(task, str(exc) or exc.__class__.__name__)
        else:
            await self._queue.complete(task.id)
            return _TaskRunResult("completed")

    async def run_until_stopped(
        self,
        *,
        poll_interval: float = 0,
        idle_sleep: float = 1,
    ) -> None:
        await self.run(
            TaskWorkerRunConfig(
                poll_interval=poll_interval,
                idle_sleep=idle_sleep,
                require_handlers=False,
            )
        )

    async def run(
        self,
        config: TaskWorkerRunConfig | None = None,
    ) -> TaskWorkerRunStats:
        run_config = config or TaskWorkerRunConfig()
        if run_config.require_handlers and not self._handlers:
            raise RuntimeError("task worker requires at least one registered handler")

        self._stopped = False
        self._stop_event = asyncio.Event()
        processed_count = 0
        completed_count = 0
        retried_count = 0
        dead_lettered_count = 0
        idle_polls = 0
        try:
            while not self._stopped:
                remaining = self._remaining_task_budget(run_config, processed_count)
                run_count = min(run_config.concurrency, remaining)
                results = await self._run_batch(run_count)
                processed_results = [result for result in results if result.processed]
                if processed_results:
                    processed_count += len(processed_results)
                    completed_count += _count_outcomes(results, "completed")
                    retried_count += _count_outcomes(results, "retried")
                    dead_lettered_count += _count_outcomes(results, "dead_lettered")
                    idle_polls = 0
                else:
                    idle_polls += 1
                    self._increment_metric("task_worker_idle_polls_total")
                if run_config.max_tasks is not None and processed_count >= run_config.max_tasks:
                    break
                if (
                    run_config.idle_poll_limit is not None
                    and idle_polls >= run_config.idle_poll_limit
                ):
                    break
                delay = run_config.poll_interval if processed_results else run_config.idle_sleep
                await self._sleep_or_stop(delay)
        finally:
            stopped = self._stopped
            self._stop_event = None
        return TaskWorkerRunStats(
            processed=processed_count,
            idle_polls=idle_polls,
            stopped=stopped,
            completed=completed_count,
            retried=retried_count,
            dead_lettered=dead_lettered_count,
        )

    def _remaining_task_budget(self, config: TaskWorkerRunConfig, processed_count: int) -> int:
        if config.max_tasks is None:
            return config.concurrency
        return max(1, config.max_tasks - processed_count)

    async def _run_batch(self, run_count: int) -> list[_TaskRunResult]:
        if run_count == 1:
            return [await self._run_once()]
        return list(await asyncio.gather(*(self._run_once() for _ in range(run_count))))

    def stop(self) -> None:
        self._stopped = True
        if self._stop_event is not None:
            self._stop_event.set()

    async def _sleep_or_stop(self, delay: float) -> None:
        if delay <= 0 or self._stopped:
            return
        event = self._stop_event
        if event is None:
            await asyncio.sleep(delay)
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=delay)
        except TimeoutError:
            return

    async def _handle_failure(self, task: TaskEnvelope, reason: str) -> _TaskRunResult:
        if task.attempts < task.max_attempts:
            await self._queue.retry(
                task.id,
                reason,
                delay_seconds=self._delay_for(task),
            )
            return _TaskRunResult("retried")
        await self._queue.dead_letter(task.id, reason)
        return _TaskRunResult("dead_lettered")

    def _delay_for(self, task: TaskEnvelope) -> float:
        if callable(self._retry_backoff):
            return max(0, float(self._retry_backoff(task)))
        return max(0, float(self._retry_backoff))

    def _record_task_result(
        self,
        task: TaskEnvelope,
        result: _TaskRunResult,
        start_time: float,
    ) -> None:
        self._increment_metric("task_worker_tasks_total")
        self._increment_metric(f"task_worker_{result.outcome}_total")
        self._increment_metric(f"task_worker_task_{_metric_safe(task.name)}_{result.outcome}_total")
        self._timing_metric("task_worker_task_seconds", time.monotonic() - start_time)

    def _increment_metric(self, name: str, amount: int = 1) -> None:
        if self._instrumentation is not None:
            self._instrumentation.increment(name, amount)

    def _timing_metric(self, name: str, value: float) -> None:
        if self._instrumentation is not None:
            self._instrumentation.timing(name, value)

    def _span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool],
    ) -> ContextManager[Any]:
        if self._instrumentation is None:
            from contextlib import nullcontext

            return nullcontext()
        return self._instrumentation.span(name, attributes)


async def run_task_worker(
    worker: TaskWorker,
    config: TaskWorkerRunConfig | None = None,
    *,
    install_signal_handlers: bool = True,
) -> TaskWorkerRunStats:
    with _task_worker_signal_handlers(worker, install_signal_handlers):
        return await worker.run(config)


@contextmanager
def _task_worker_signal_handlers(worker: TaskWorker, enabled: bool):
    if not enabled:
        yield
        return
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, worker.stop)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(signum)
    try:
        yield
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def _count_outcomes(results: list[_TaskRunResult], outcome: TaskRunOutcome) -> int:
    return sum(1 for result in results if result.outcome == outcome)


def _metric_safe(value: str) -> str:
    return (
        "".join(character if character.isalnum() else "_" for character in value).strip("_") or "_"
    )
