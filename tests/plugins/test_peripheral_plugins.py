import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.manager import PluginManager
from infra.plugins.notifications import NoopNotificationService, NotificationsPlugin
from infra.plugins.payment import MockPaymentService, PaymentPlugin
from infra.plugins.ratelimit import MemoryRateLimiter, RateLimitPlugin
from infra.plugins.storage import LocalStorage, StoragePlugin
from infra.plugins.webhooks import WebhookDispatcher, WebhooksPlugin


@pytest.mark.asyncio
async def test_local_storage_reads_writes_deletes_and_blocks_path_traversal(tmp_path):
    storage = LocalStorage(tmp_path)

    await storage.write_bytes("nested/file.bin", b"payload")

    assert await storage.exists("nested/file.bin") is True
    assert await storage.read_bytes("nested/file.bin") == b"payload"

    with pytest.raises(ValueError, match="storage key escapes root"):
        await storage.write_bytes("../escape.bin", b"bad")

    await storage.delete("nested/file.bin")
    assert await storage.exists("nested/file.bin") is False


@pytest.mark.asyncio
async def test_webhook_dispatcher_invokes_registered_async_handlers():
    dispatcher = WebhookDispatcher()

    async def first(event, payload):
        return {"handler": "first", "event": event, "value": payload["value"]}

    async def second(event, payload):
        return f"{event}:{payload['value']}"

    dispatcher.register(first)
    dispatcher.register(second)

    assert await dispatcher.dispatch("created", {"value": 3}) == [
        {"handler": "first", "event": "created", "value": 3},
        "created:3",
    ]


@pytest.mark.asyncio
async def test_mock_payment_service_creates_checkout():
    service = MockPaymentService()

    checkout = await service.create_checkout(
        amount=1250,
        currency="usd",
        reference="order-123",
    )

    assert checkout.amount == 1250
    assert checkout.currency == "USD"
    assert checkout.reference == "order-123"
    assert checkout.status == "pending"
    assert checkout.url.endswith(checkout.id)


@pytest.mark.asyncio
async def test_memory_rate_limiter_rejects_after_limit_within_window():
    limiter = MemoryRateLimiter()

    assert await limiter.allow("client", limit=2, window_seconds=60) is True
    assert await limiter.allow("client", limit=2, window_seconds=60) is True
    assert await limiter.allow("client", limit=2, window_seconds=60) is False


@pytest.mark.asyncio
async def test_noop_notification_service_records_skipped_send():
    service = NoopNotificationService()

    result = await service.send(
        channel="email",
        recipient="user@example.com",
        subject="Subject",
        body="Body",
        metadata={"trace": "abc"},
    )

    assert result.status == "skipped"
    assert result.channel == "email"
    assert result.recipient == "user@example.com"
    assert service.results == [result]


@pytest.mark.asyncio
async def test_peripheral_plugins_register_services_through_plugin_manager(tmp_path):
    settings = InfraSettings(
        infra={
            "plugins": {
                "storage": {
                    "enabled": True,
                    "config": {"root": str(tmp_path / "storage")},
                },
                "webhooks": {"enabled": True},
                "payment": {"enabled": True},
                "ratelimit": {"enabled": True},
                "notifications": {"enabled": True},
            }
        }
    )
    manager = PluginManager(
        settings=settings,
        plugins=[
            StoragePlugin(),
            WebhooksPlugin(),
            PaymentPlugin(),
            RateLimitPlugin(),
            NotificationsPlugin(),
        ],
    )

    await manager.startup()

    assert isinstance(manager.get("storage"), LocalStorage)
    assert isinstance(manager.get("webhooks"), WebhookDispatcher)
    assert isinstance(manager.get("payment"), MockPaymentService)
    assert isinstance(manager.get("ratelimit"), MemoryRateLimiter)
    assert isinstance(manager.get("notifications"), NoopNotificationService)
    assert {
        name: status.status for name, status in manager.health.snapshot().items()
    } == {
        "storage": HealthState.HEALTHY,
        "webhooks": HealthState.HEALTHY,
        "payment": HealthState.HEALTHY,
        "ratelimit": HealthState.HEALTHY,
        "notifications": HealthState.HEALTHY,
    }

    await manager.shutdown()
