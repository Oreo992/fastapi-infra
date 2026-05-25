import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from infra.plugins.tasks.adapters._broker import BrokerMessage, BrokerTaskQueue
from infra.plugins.tasks.models import TaskEnvelope


class SqsTaskQueue(BrokerTaskQueue):
    name = "sqs"

    def __init__(
        self,
        *,
        queue_url: str,
        client: Any | None = None,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
        wait_time_seconds: int = 0,
        visibility_timeout: int | None = None,
        message_group_id: str | None = None,
        dead_letter_queue_url: str | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(now=now)
        self._queue_url = queue_url
        self._client = client or _create_boto3_sqs_client(
            region_name=region_name,
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
        )
        self._wait_time_seconds = wait_time_seconds
        self._visibility_timeout = visibility_timeout
        self._message_group_id = message_group_id
        self._dead_letter_queue_url = dead_letter_queue_url

    async def _send(self, task: TaskEnvelope, *, delay_seconds: float = 0) -> None:
        kwargs: dict[str, Any] = {
            "QueueUrl": self._queue_url,
            "MessageBody": task.model_dump_json(),
            "DelaySeconds": min(900, int(delay_seconds)),
        }
        if self._message_group_id is not None:
            kwargs["MessageGroupId"] = self._message_group_id
            kwargs["MessageDeduplicationId"] = task.idempotency_key or task.id
        await self._call("send_message", **kwargs)

    async def _receive(self) -> BrokerMessage | None:
        kwargs: dict[str, Any] = {
            "QueueUrl": self._queue_url,
            "MaxNumberOfMessages": 1,
            "WaitTimeSeconds": self._wait_time_seconds,
        }
        if self._visibility_timeout is not None:
            kwargs["VisibilityTimeout"] = self._visibility_timeout
        response = await self._call("receive_message", **kwargs)
        messages = response.get("Messages", []) if isinstance(response, dict) else []
        if not messages:
            return None
        message = messages[0]
        task = TaskEnvelope.model_validate_json(message["Body"])
        return BrokerMessage(task=task, receipt=message["ReceiptHandle"])

    async def _ack(self, receipt: str) -> None:
        await self._call("delete_message", QueueUrl=self._queue_url, ReceiptHandle=receipt)

    async def _defer(self, message: BrokerMessage) -> None:
        remaining = max(0, int(message.task.available_at - self._now()))
        await self._call(
            "change_message_visibility",
            QueueUrl=self._queue_url,
            ReceiptHandle=message.receipt,
            VisibilityTimeout=min(43_200, remaining),
        )
        self._receipts.pop(message.task.id, None)

    async def _send_dead_letter(self, task: TaskEnvelope) -> None:
        if self._dead_letter_queue_url is None:
            return None
        kwargs: dict[str, Any] = {
            "QueueUrl": self._dead_letter_queue_url,
            "MessageBody": task.model_dump_json(),
        }
        if self._message_group_id is not None:
            kwargs["MessageGroupId"] = self._message_group_id
            kwargs["MessageDeduplicationId"] = task.id
        await self._call("send_message", **kwargs)
        return None

    async def _health_check(self) -> bool:
        await self._call(
            "get_queue_attributes", QueueUrl=self._queue_url, AttributeNames=["QueueArn"]
        )
        return True

    async def _call(self, method_name: str, **kwargs: Any) -> Any:
        method = getattr(self._client, method_name)
        if inspect.iscoroutinefunction(method):
            return await method(**kwargs)
        return await asyncio.to_thread(method, **kwargs)


def _create_boto3_sqs_client(**kwargs: Any) -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("SQS task backend requires the 'boto3' package") from exc
    return boto3.client("sqs", **{key: value for key, value in kwargs.items() if value is not None})
