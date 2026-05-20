from collections.abc import Awaitable, Callable
from typing import Any, ContextManager, Protocol, runtime_checkable

from infra.core.services import ServiceKey


@runtime_checkable
class _AIService(Protocol):
    def names(self) -> list[str]: ...


@runtime_checkable
class _AuthService(Protocol):
    def authenticate_api_key(self, api_key: str | None) -> object: ...


@runtime_checkable
class _CacheService(Protocol):
    async def get(self, key: str) -> object: ...

    async def set(self, key: str, value: Any, ttl: int = 3600) -> bool: ...


@runtime_checkable
class _DatabaseService(Protocol):
    async def initialize(self) -> None: ...

    async def put_document(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def get_document(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def delete_document(self, collection: str, key: str) -> bool: ...


@runtime_checkable
class _HttpService(Protocol):
    async def request(self, method: str, url: str, **kwargs: Any) -> object: ...


@runtime_checkable
class _ObservabilityService(Protocol):
    def increment(self, name: str, amount: int = 1) -> None: ...

    def timing(self, name: str, value: float) -> None: ...

    def span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> ContextManager[Any]: ...


@runtime_checkable
class _PaymentService(Protocol):
    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
        provider: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> object: ...


@runtime_checkable
class _SpeechService(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        *,
        format: str = "wav",
        language: str | None = None,
        model: str = "mock-asr",
        provider: str | None = None,
    ) -> object: ...


@runtime_checkable
class _TaskQueueService(Protocol):
    async def enqueue(
        self,
        name: str,
        payload: dict[str, object] | None = None,
        *,
        idempotency_key: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int = 1,
    ) -> object: ...


WebhookHandler = Callable[[str, Any], Awaitable[Any]]


@runtime_checkable
class _WebhookService(Protocol):
    def register(self, handler: WebhookHandler) -> None: ...


@runtime_checkable
class NotificationService(Protocol):
    async def send(
        self,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> object: ...


@runtime_checkable
class RateLimiterService(Protocol):
    async def allow(self, key: str, limit: int, window_seconds: float) -> bool: ...


@runtime_checkable
class StorageService(Protocol):
    async def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    async def get_object(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def delete_object(self, key: str) -> None: ...

    async def list_objects(self, prefix: str = "") -> list[str]: ...


AI_SERVICE = ServiceKey[_AIService]("ai", _AIService)
AUTH_SERVICE = ServiceKey[_AuthService]("auth", _AuthService)
CACHE_SERVICE = ServiceKey[_CacheService]("cache", _CacheService)
DATABASE_SERVICE = ServiceKey[_DatabaseService]("database", _DatabaseService)
HTTP_SERVICE = ServiceKey[_HttpService]("http", _HttpService)
NOTIFICATIONS_SERVICE = ServiceKey[NotificationService](
    "notifications",
    NotificationService,
)
OBSERVABILITY_SERVICE = ServiceKey[_ObservabilityService](
    "observability",
    _ObservabilityService,
)
PAYMENT_SERVICE = ServiceKey[_PaymentService]("payment", _PaymentService)
RATELIMIT_SERVICE = ServiceKey[RateLimiterService]("ratelimit", RateLimiterService)
SPEECH_SERVICE = ServiceKey[_SpeechService]("speech", _SpeechService)
STORAGE_SERVICE = ServiceKey[StorageService]("storage", StorageService)
TASKS_SERVICE = ServiceKey[_TaskQueueService]("tasks", _TaskQueueService)
WEBHOOKS_SERVICE = ServiceKey[_WebhookService]("webhooks", _WebhookService)


__all__ = [
    "AI_SERVICE",
    "AUTH_SERVICE",
    "CACHE_SERVICE",
    "DATABASE_SERVICE",
    "HTTP_SERVICE",
    "NOTIFICATIONS_SERVICE",
    "NotificationService",
    "OBSERVABILITY_SERVICE",
    "PAYMENT_SERVICE",
    "RATELIMIT_SERVICE",
    "RateLimiterService",
    "SPEECH_SERVICE",
    "STORAGE_SERVICE",
    "StorageService",
    "TASKS_SERVICE",
    "WEBHOOKS_SERVICE",
]
