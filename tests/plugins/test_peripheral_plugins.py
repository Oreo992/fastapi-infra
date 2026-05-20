from typing import Any

import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.manager import PluginManager
from infra.plugins.notifications import (
    NoopNotificationService,
    NotificationProviderRegistry,
    NotificationsPlugin,
    SMTPNotificationConfig,
    SMTPNotificationError,
    SMTPNotificationService,
    WebhookNotificationConfig,
    WebhookNotificationError,
    WebhookNotificationService,
)
from infra.plugins.payment import (
    InMemoryPaymentStore,
    MockPaymentProvider,
    PaymentCheckout,
    PaymentPlugin,
    PaymentProviderRegistry,
    PaymentRefund,
    PaymentService,
    SqlPaymentStore,
    StripePaymentProvider,
)
from infra.plugins.ratelimit import (
    MemoryRateLimiter,
    RateLimitBackendRegistry,
    RateLimitPlugin,
    RedisRateLimiter,
)
from infra.plugins.storage import LocalStorage, S3Storage, StoragePlugin, StorageProviderRegistry
from infra.plugins.webhooks import WebhookDispatcher, WebhooksPlugin


class FakeRateLimitRedis:
    def __init__(self, *, ping_ok: bool = True) -> None:
        self.values: dict[str, int] = {}
        self.expirations: dict[str, int] = {}
        self.ping_ok = ping_ok

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expirations[key] = seconds
        return True

    async def ping(self) -> bool:
        return self.ping_ok


class FakeRateLimitDatabaseService:
    def __init__(self, redis: FakeRateLimitRedis) -> None:
        self.redis = redis

    async def get_redis_client(self) -> FakeRateLimitRedis:
        return self.redis


class FakeRateLimitDatabasePlugin:
    metadata = PluginMetadata(name="database", version="1.0.0", provides=["database"])
    config_model = None

    def __init__(self, redis: FakeRateLimitRedis) -> None:
        self.redis = redis

    def register(self, ctx: PluginContext) -> None:
        ctx.services["database"] = FakeRateLimitDatabaseService(self.redis)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("database", HealthState.HEALTHY)


class FakeWebhookResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeWebhookOpener:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [200])
        self.requests: list[tuple[Any, float]] = []

    def __call__(self, request: Any, timeout: float) -> FakeWebhookResponse:
        self.requests.append((request, timeout))
        status = self.statuses.pop(0) if self.statuses else 200
        return FakeWebhookResponse(status)


@pytest.mark.asyncio
async def test_local_storage_reads_writes_deletes_and_blocks_path_traversal(tmp_path):
    storage = LocalStorage(tmp_path)

    await storage.put_object("nested/file.bin", b"payload")
    await storage.put_object("nested/other.txt", b"other")
    await storage.put_object("top-level.txt", b"top")

    assert await storage.exists("nested/file.bin") is True
    assert await storage.get_object("nested/file.bin") == b"payload"
    assert await storage.list_objects() == [
        "nested/file.bin",
        "nested/other.txt",
        "top-level.txt",
    ]
    assert await storage.list_objects("nested/") == [
        "nested/file.bin",
        "nested/other.txt",
    ]

    with pytest.raises(NotImplementedError, match="LocalStorage does not support presigned URLs"):
        storage.presign_get_url("nested/file.bin")

    with pytest.raises(ValueError, match="storage key escapes root"):
        await storage.put_object("../escape.bin", b"bad")

    await storage.delete_object("nested/file.bin")
    assert await storage.exists("nested/file.bin") is False


@pytest.mark.asyncio
async def test_storage_plugin_registers_s3_provider_when_configured():
    settings = InfraSettings(
        infra={
            "plugins": {
                "storage": {
                    "enabled": True,
                    "config": {
                        "default_provider": "s3",
                        "providers": {
                            "s3": {
                                "bucket": "assets",
                                "region": "us-east-1",
                                "access_key_id": "AKIDEXAMPLE",
                                "secret_access_key": "secret",
                                "endpoint_url": "https://s3.example.test",
                                "force_path_style": True,
                            }
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[StoragePlugin()])

    await manager.startup()

    storage = manager.get("storage")
    assert isinstance(storage, StorageProviderRegistry)
    assert isinstance(storage.get(), S3Storage)
    assert manager.health.snapshot()["storage"].status is HealthState.DEGRADED

    await manager.shutdown()


class FakeStorageEntryPoint:
    def __init__(self, name: str, loaded: object) -> None:
        self.name = name
        self.loaded = loaded

    def load(self) -> object:
        return self.loaded


class CustomRateLimiter:
    name = "custom"

    def __init__(self, config):
        self.config = dict(config)
        self.calls = []

    async def allow(self, key, limit, window_seconds):
        self.calls.append((key, limit, window_seconds))
        return True


class CustomStorageProvider:
    name = "custom"

    def __init__(self, config):
        self.config = dict(config)
        self.objects: dict[str, bytes] = {}

    async def put_object(self, key, data, content_type=None, metadata=None):
        self.objects[key] = bytes(data)

    async def get_object(self, key):
        return self.objects[key]

    async def exists(self, key):
        return key in self.objects

    async def delete_object(self, key):
        self.objects.pop(key, None)

    async def list_objects(self, prefix=""):
        return sorted(key for key in self.objects if key.startswith(prefix))


@pytest.mark.asyncio
async def test_storage_plugin_loads_external_provider_from_entry_point(monkeypatch):
    def provider_factory(config):
        return CustomStorageProvider(config)

    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeStorageEntryPoint("custom", provider_factory)],
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "storage": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {"bucket": "assets"}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[StoragePlugin()])

    await manager.startup()

    storage = manager.get("storage")
    assert isinstance(storage, StorageProviderRegistry)
    assert storage.names() == ["custom"]
    provider = storage.get()
    assert isinstance(provider, CustomStorageProvider)
    assert provider.config == {"bucket": "assets"}
    await storage.put_object("demo.txt", b"demo")
    assert await storage.get_object("demo.txt") == b"demo"

    await manager.shutdown()


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
async def test_payment_service_creates_default_mock_checkout_and_reads_status():
    registry = PaymentProviderRegistry(default_provider="mock")
    registry.register(MockPaymentProvider())
    service = PaymentService(registry)

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

    status = await service.get_payment_status(checkout.id)
    refund = await service.create_refund(
        checkout_id=checkout.id,
        amount=500,
        currency="usd",
        reference="refund-order-123",
    )

    assert status == "pending"
    assert refund.checkout_id == checkout.id
    assert refund.amount == 500
    assert refund.currency == "USD"
    assert refund.reference == "refund-order-123"
    assert refund.status == "succeeded"
    assert await service.get_payment_status(checkout.id) == "refunded"


@pytest.mark.asyncio
async def test_payment_service_records_provider_results_when_store_is_configured():
    registry = PaymentProviderRegistry(default_provider="mock")
    registry.register(MockPaymentProvider())
    store = InMemoryPaymentStore()
    service = PaymentService(registry, store=store)

    checkout = await service.create_checkout(amount=1250, currency="usd")
    await service.get_checkout(checkout.id)
    refund = await service.create_refund(checkout.id, amount=500, currency="usd")

    assert store.checkouts[("mock", checkout.id)].id == checkout.id
    assert store.refunds[("mock", refund.id)].checkout_id == checkout.id


class FakePaymentDatabase:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any, bool]] = []

    async def execute_sql(self, sql: str, params: Any = None, commit: bool = True) -> int:
        self.statements.append((sql, params, commit))
        return 1


class FakeDatabasePlugin:
    metadata = PluginMetadata(
        name="database",
        version="1.0.0",
        default_enabled=False,
        provides=["database"],
    )
    config_model = None

    def __init__(self, database):
        self.database = database

    def register(self, ctx: PluginContext) -> None:
        ctx.services["database"] = self.database

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("database", HealthState.HEALTHY)


@pytest.mark.asyncio
async def test_payment_plugin_can_attach_sql_store_from_configured_service():
    database = FakePaymentDatabase()
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {"enabled": True},
                "payment": {
                    "enabled": True,
                    "config": {"default_provider": "mock", "store_service": "database"},
                },
            }
        }
    )
    manager = PluginManager(
        settings=settings,
        plugins=[FakeDatabasePlugin(database), PaymentPlugin()],
    )

    await manager.startup()
    service = manager.get("payment")
    assert isinstance(service, PaymentService)
    assert isinstance(service.store, SqlPaymentStore)

    await service.create_checkout(amount=1250, currency="usd")
    await manager.shutdown()

    assert any("INSERT INTO infra_payment_checkouts" in sql for sql, _, _ in database.statements)


@pytest.mark.asyncio
async def test_payment_plugin_requires_sql_capable_store_service():
    class NotDatabase:
        pass

    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {"enabled": True},
                "payment": {
                    "enabled": True,
                    "config": {"default_provider": "mock", "store_service": "database"},
                },
            }
        }
    )
    manager = PluginManager(
        settings=settings,
        plugins=[FakeDatabasePlugin(NotDatabase()), PaymentPlugin()],
    )

    with pytest.raises(RuntimeError, match="must expose execute_sql"):
        await manager.startup()


@pytest.mark.asyncio
async def test_payment_plugin_reports_external_provider_as_degraded_until_live_checked():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "providers": {"stripe": {"api_key": "sk-test"}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[PaymentPlugin()])

    await manager.startup()
    service = manager.get("payment")
    await manager.shutdown()

    assert isinstance(service, PaymentService)
    assert manager.health.snapshot()["payment"].status is HealthState.DEGRADED


@pytest.mark.asyncio
async def test_payment_plugin_health_probe_checks_non_default_external_providers(monkeypatch):
    from infra.core.health import HealthStatus

    async def fake_health_check(self):
        return HealthStatus(
            name="stripe",
            status=HealthState.UNHEALTHY,
            message="stripe unavailable",
            details={"provider": "stripe"},
        )

    monkeypatch.setattr(StripePaymentProvider, "health_check", fake_health_check)
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "mock",
                        "providers": {"stripe": {"api_key": "sk-test"}},
                        "health_probe": True,
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[PaymentPlugin()])

    with pytest.raises(Exception, match="plugin is unhealthy: payment"):
        await manager.startup()

    status = manager.health.snapshot()["payment"]
    assert status.status is HealthState.UNHEALTHY
    assert status.message == "stripe unavailable"
    assert status.details["providers"]["stripe"]["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_sql_payment_store_creates_tables_and_upserts_records():
    database = FakePaymentDatabase()
    store = SqlPaymentStore(database)
    checkout = PaymentCheckout(
        id="cs_123",
        amount=1250,
        currency="USD",
        reference="order-123",
        status="pending",
        url="https://checkout.example/cs_123",
    )
    refund = PaymentRefund(
        id="rf_123",
        checkout_id="cs_123",
        amount=500,
        currency="USD",
        reference="refund-order-123",
        status="succeeded",
    )

    await store.save_checkout("stripe", checkout)
    await store.save_refund("stripe", refund)

    assert len(database.statements) == 4
    assert "CREATE TABLE IF NOT EXISTS infra_payment_checkouts" in database.statements[0][0]
    assert "CREATE TABLE IF NOT EXISTS infra_payment_refunds" in database.statements[1][0]
    assert "INSERT INTO infra_payment_checkouts" in database.statements[2][0]
    assert "ON DUPLICATE KEY UPDATE" in database.statements[2][0]
    assert database.statements[2][1][0:3] == ("stripe", "cs_123", 1250)
    assert "INSERT INTO infra_payment_refunds" in database.statements[3][0]
    assert database.statements[3][1][0:3] == ("stripe", "rf_123", "cs_123")


def test_sql_payment_store_rejects_unsafe_table_names():
    with pytest.raises(ValueError, match="checkout_table"):
        SqlPaymentStore(FakePaymentDatabase(), checkout_table="payments; DROP TABLE users")


class CustomPaymentProvider:
    name = "custom"

    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentCheckout:
        return PaymentCheckout(
            id="custom-checkout",
            amount=amount,
            currency=currency.lower(),
            reference=reference,
            status="custom-pending",
            url="custom://checkout/custom-checkout",
        )

    async def get_checkout(self, checkout_id: str) -> PaymentCheckout:
        return PaymentCheckout(
            id=checkout_id,
            amount=500,
            currency="usd",
            reference=None,
            status="custom-pending",
            url=f"custom://checkout/{checkout_id}",
        )

    async def get_payment_status(self, checkout_id: str) -> str:
        return f"custom-status:{checkout_id}"

    async def create_refund(
        self,
        checkout_id: str,
        amount: int,
        currency: str,
        reference: str | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentRefund:
        return PaymentRefund(
            id="custom-refund",
            checkout_id=checkout_id,
            amount=amount,
            currency=currency.lower(),
            reference=reference,
            status="custom-refunded",
        )


@pytest.mark.asyncio
async def test_payment_service_supports_provider_override():
    registry = PaymentProviderRegistry(default_provider="mock")
    registry.register(CustomPaymentProvider())
    service = PaymentService(registry)

    checkout = await service.create_checkout(
        amount=500,
        currency="USD",
        provider="custom",
    )
    status = await service.get_payment_status(checkout.id, provider="custom")
    refund = await service.create_refund(
        checkout_id=checkout.id,
        amount=250,
        currency="USD",
        reference="custom-ref",
        provider="custom",
    )

    assert checkout.id == "custom-checkout"
    assert checkout.currency == "usd"
    assert status == "custom-status:custom-checkout"
    assert refund.id == "custom-refund"
    assert refund.currency == "usd"
    assert refund.status == "custom-refunded"


@pytest.mark.asyncio
async def test_payment_plugin_rejects_unknown_provider_config_through_manager():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[PaymentPlugin()])

    with pytest.raises(ValueError, match="unknown payment provider"):
        await manager.startup()


class FakeProviderEntryPoint:
    name = "custom"

    def load(self):
        return lambda config: CustomPaymentProvider()


class BrokenPaymentProvider:
    name = "custom"

    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentCheckout:
        return PaymentCheckout(
            id="custom-checkout",
            amount=amount,
            currency=currency.lower(),
            reference=reference,
            status="custom-pending",
            url="custom://checkout/custom-checkout",
        )


class BrokenPaymentProviderEntryPoint:
    name = "custom"

    def load(self):
        return lambda config: BrokenPaymentProvider()


@pytest.mark.asyncio
async def test_payment_plugin_registers_custom_entry_point_provider(monkeypatch):
    import infra.plugins.provider_extensions as provider_extensions

    monkeypatch.setattr(
        provider_extensions,
        "entry_points",
        lambda group: (
            [FakeProviderEntryPoint()] if group == "fastapi_infra.payment_providers" else []
        ),
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[PaymentPlugin()])

    await manager.startup()
    service = manager.get("payment")
    checkout = await service.create_checkout(amount=500, currency="USD")

    assert checkout.id == "custom-checkout"
    assert manager.health.snapshot()["payment"].status is HealthState.DEGRADED


@pytest.mark.asyncio
async def test_payment_plugin_rejects_custom_provider_missing_required_methods(monkeypatch):
    import infra.plugins.provider_extensions as provider_extensions

    monkeypatch.setattr(
        provider_extensions,
        "entry_points",
        lambda group: (
            [BrokenPaymentProviderEntryPoint()]
            if group == "fastapi_infra.payment_providers"
            else []
        ),
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[PaymentPlugin()])

    with pytest.raises(
        ValueError,
        match=(
            "fastapi_infra.payment_providers:custom provider is missing required "
            r"method\(s\): get_checkout, get_payment_status, create_refund"
        ),
    ):
        await manager.startup()


@pytest.mark.asyncio
async def test_memory_rate_limiter_rejects_after_limit_within_window():
    limiter = MemoryRateLimiter()

    assert await limiter.allow("client", limit=2, window_seconds=60) is True
    assert await limiter.allow("client", limit=2, window_seconds=60) is True
    assert await limiter.allow("client", limit=2, window_seconds=60) is False


@pytest.mark.asyncio
async def test_redis_rate_limiter_rejects_after_limit_within_fixed_window():
    redis = FakeRateLimitRedis()
    limiter = RedisRateLimiter(redis, key_prefix="test:ratelimit", now=lambda: 120.0)

    assert await limiter.allow("client", limit=2, window_seconds=60) is True
    assert await limiter.allow("client", limit=2, window_seconds=60) is True
    assert await limiter.allow("client", limit=2, window_seconds=60) is False
    assert redis.values["test:ratelimit:client:60:2"] == 3
    assert redis.expirations["test:ratelimit:client:60:2"] == 60


@pytest.mark.asyncio
async def test_rate_limit_plugin_registers_redis_backend_from_database_service():
    redis = FakeRateLimitRedis()
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {"enabled": True},
                "ratelimit": {
                    "enabled": True,
                    "config": {
                        "default_provider": "redis",
                        "providers": {"redis": {"key_prefix": "test:ratelimit"}},
                    },
                },
            }
        }
    )
    manager = PluginManager(
        settings=settings,
        plugins=[FakeRateLimitDatabasePlugin(redis), RateLimitPlugin()],
    )

    await manager.startup()

    limiter = manager.get("ratelimit")
    assert isinstance(limiter, RateLimitBackendRegistry)
    assert isinstance(limiter.provider(), RedisRateLimiter)
    assert await limiter.allow("client", limit=1, window_seconds=60) is True
    assert await limiter.allow("client", limit=1, window_seconds=60) is False
    assert manager.health.snapshot()["ratelimit"].status == HealthState.HEALTHY


@pytest.mark.asyncio
async def test_rate_limit_plugin_reports_unhealthy_when_redis_health_check_fails():
    redis = FakeRateLimitRedis(ping_ok=False)
    settings = InfraSettings(
        infra={
            "plugins": {
                "ratelimit": {
                    "enabled": True,
                    "config": {"default_provider": "redis"},
                },
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[RateLimitPlugin(redis=redis)])

    with pytest.raises(Exception, match="plugin is unhealthy: ratelimit"):
        await manager.startup()

    status = manager.health.snapshot()["ratelimit"]
    assert status.status == HealthState.UNHEALTHY
    assert status.message == "rate limiter health check failed"


@pytest.mark.asyncio
async def test_rate_limit_plugin_loads_external_provider_from_entry_point(monkeypatch):
    def provider_factory(config):
        return CustomRateLimiter(config)

    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeStorageEntryPoint("custom", provider_factory)],
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "ratelimit": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {"window": "edge"}},
                    },
                },
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[RateLimitPlugin()])

    await manager.startup()

    limiter = manager.get("ratelimit")
    assert isinstance(limiter, RateLimitBackendRegistry)
    provider = limiter.provider()
    assert isinstance(provider, CustomRateLimiter)
    assert provider.config == {"window": "edge"}
    assert await limiter.allow("client", limit=1, window_seconds=60) is True
    assert provider.calls == [("client", 1, 60)]


@pytest.mark.asyncio
async def test_rate_limit_plugin_requires_redis_backing_for_redis_provider():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ratelimit": {
                    "enabled": True,
                    "config": {"default_provider": "redis"},
                },
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[RateLimitPlugin()])

    with pytest.raises(RuntimeError, match="Redis rate limit provider requires"):
        await manager.startup()


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


class FakeSMTPClient:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.messages: list[Any] = []
        self.quit_called = False

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message) -> None:
        self.messages.append(message)

    def quit(self) -> None:
        self.quit_called = True


class FailingOnceSMTPFactory:
    def __init__(self) -> None:
        self.clients: list[FakeSMTPClient] = []
        self.calls = 0

    def __call__(self, host: str, port: int, timeout: float) -> FakeSMTPClient:
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary smtp outage")
        client = FakeSMTPClient(host, port, timeout)
        self.clients.append(client)
        return client


class CustomNotificationProvider:
    name = "custom"

    def __init__(self, config):
        self.config = dict(config)
        self.messages = []

    async def send(self, channel, recipient, subject, body, metadata=None):
        result = {
            "channel": channel,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "metadata": metadata or {},
            "status": "sent",
        }
        self.messages.append(result)
        return result


@pytest.mark.asyncio
async def test_smtp_notification_service_sends_real_email_message():
    clients = []

    def factory(host: str, port: int, timeout: float) -> FakeSMTPClient:
        client = FakeSMTPClient(host, port, timeout)
        clients.append(client)
        return client

    service = SMTPNotificationService(
        SMTPNotificationConfig(
            host="smtp.example.test",
            port=2525,
            sender="noreply@example.test",
            username="mailer",
            password="secret",
            timeout=12.0,
        ),
        smtp_factory=factory,
    )

    result = await service.send(
        channel="email",
        recipient="user@example.test",
        subject="Subject",
        body="Body",
        metadata={"trace": "abc"},
    )

    client = clients[0]
    message = client.messages[0]
    assert client.host == "smtp.example.test"
    assert client.port == 2525
    assert client.timeout == 12.0
    assert client.started_tls is True
    assert client.login_args == ("mailer", "secret")
    assert client.quit_called is True
    assert message["From"] == "noreply@example.test"
    assert message["To"] == "user@example.test"
    assert message["Subject"] == "Subject"
    assert message.get_content().strip() == "Body"
    assert result.status == "sent"
    assert result.metadata == {"trace": "abc"}


@pytest.mark.asyncio
async def test_smtp_notification_service_retries_temporary_connection_errors():
    factory = FailingOnceSMTPFactory()
    service = SMTPNotificationService(
        SMTPNotificationConfig(
            host="smtp.example.test",
            port=2525,
            sender="noreply@example.test",
            max_attempts=2,
            retry_base_delay=0,
        ),
        smtp_factory=factory,
    )

    result = await service.send(
        channel="email",
        recipient="user@example.test",
        subject="Subject",
        body="Body",
    )

    assert result.status == "sent"
    assert factory.calls == 2
    assert len(factory.clients[0].messages) == 1


@pytest.mark.asyncio
async def test_smtp_notification_service_does_not_retry_authentication_errors():
    calls = 0

    class AuthFailingClient(FakeSMTPClient):
        def login(self, username: str, password: str) -> None:
            raise SMTPNotificationError("auth failed", retryable=False)

    def factory(host: str, port: int, timeout: float) -> FakeSMTPClient:
        nonlocal calls
        calls += 1
        return AuthFailingClient(host, port, timeout)

    service = SMTPNotificationService(
        SMTPNotificationConfig(
            host="smtp.example.test",
            port=2525,
            sender="noreply@example.test",
            username="mailer",
            password="bad",
            max_attempts=2,
            retry_base_delay=0,
        ),
        smtp_factory=factory,
    )

    with pytest.raises(SMTPNotificationError, match="auth failed"):
        await service.send(
            channel="email",
            recipient="user@example.test",
            subject="Subject",
            body="Body",
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_smtp_notification_service_health_check_connects_without_sending():
    clients = []

    def factory(host: str, port: int, timeout: float) -> FakeSMTPClient:
        client = FakeSMTPClient(host, port, timeout)
        clients.append(client)
        return client

    service = SMTPNotificationService(
        SMTPNotificationConfig(
            host="smtp.example.test",
            port=2525,
            sender="noreply@example.test",
            username="mailer",
            password="secret",
            timeout=12.0,
        ),
        smtp_factory=factory,
    )

    status = await service.health_check()

    client = clients[0]
    assert status.status is HealthState.HEALTHY
    assert status.details == {"provider": "smtp", "host": "smtp.example.test", "port": 2525}
    assert client.started_tls is True
    assert client.login_args == ("mailer", "secret")
    assert client.messages == []
    assert client.quit_called is True


@pytest.mark.asyncio
async def test_notifications_plugin_registers_smtp_provider_when_configured():
    settings = InfraSettings(
        infra={
            "plugins": {
                "notifications": {
                    "enabled": True,
                    "config": {
                        "default_provider": "smtp",
                        "providers": {
                            "smtp": {
                                "host": "smtp.example.test",
                                "sender": "noreply@example.test",
                            }
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[NotificationsPlugin()])

    await manager.startup()

    notifications = manager.get("notifications")
    assert isinstance(notifications, NotificationProviderRegistry)
    assert isinstance(notifications.get(), SMTPNotificationService)
    assert manager.health.snapshot()["notifications"].status is HealthState.DEGRADED

    await manager.shutdown()


@pytest.mark.asyncio
async def test_notifications_plugin_loads_external_provider_from_entry_point(monkeypatch):
    def provider_factory(config):
        return CustomNotificationProvider(config)

    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeStorageEntryPoint("custom", provider_factory)],
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "notifications": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {"api_key": "test-key"}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[NotificationsPlugin()])

    await manager.startup()

    notifications = manager.get("notifications")
    assert isinstance(notifications, NotificationProviderRegistry)
    assert notifications.names() == ["custom"]
    provider = notifications.get()
    assert isinstance(provider, CustomNotificationProvider)
    assert provider.config == {"api_key": "test-key"}
    result = await notifications.send("sms", "+15555550123", "Code", "123456")
    assert result["status"] == "sent"
    assert provider.messages == [result]

    await manager.shutdown()


@pytest.mark.asyncio
async def test_webhook_notification_service_posts_signed_json_payload():
    opener = FakeWebhookOpener()
    service = WebhookNotificationService(
        WebhookNotificationConfig(
            url="https://hooks.example.test/notify",
            health_url="https://hooks.example.test/health",
            signing_secret="hook-secret",
            timeout=12.0,
            headers={"x-app": "billing"},
        ),
        opener=opener,
    )

    result = await service.send(
        channel="webhook",
        recipient="billing-system",
        subject="invoice.paid",
        body="Invoice paid",
        metadata={"invoice_id": "inv_123"},
    )

    request, timeout = opener.requests[0]
    assert result.status == "sent"
    assert timeout == 12.0
    assert request.full_url == "https://hooks.example.test/notify"
    assert request.get_method() == "POST"
    assert b'"invoice_id":"inv_123"' in request.data
    assert request.headers["Content-type"] == "application/json"
    assert request.headers["X-app"] == "billing"
    assert "X-infra-signature" in request.headers
    assert "X-infra-timestamp" in request.headers


@pytest.mark.asyncio
async def test_webhook_notification_service_retries_retryable_status():
    opener = FakeWebhookOpener([500, 200])
    service = WebhookNotificationService(
        WebhookNotificationConfig(
            url="https://hooks.example.test/notify",
            max_attempts=2,
            retry_base_delay=0,
        ),
        opener=opener,
    )

    result = await service.send("webhook", "system", "event", "body")

    assert result.status == "sent"
    assert len(opener.requests) == 2


@pytest.mark.asyncio
async def test_webhook_notification_service_health_uses_health_url():
    opener = FakeWebhookOpener()
    service = WebhookNotificationService(
        WebhookNotificationConfig(
            url="https://hooks.example.test/notify",
            health_url="https://hooks.example.test/health",
        ),
        opener=opener,
    )

    status = await service.health_check()

    request, _timeout = opener.requests[0]
    assert status.status is HealthState.HEALTHY
    assert request.full_url == "https://hooks.example.test/health"
    assert request.get_method() == "GET"


@pytest.mark.asyncio
async def test_webhook_notification_service_health_is_degraded_without_health_url():
    service = WebhookNotificationService(
        WebhookNotificationConfig(url="https://hooks.example.test/notify"),
        opener=FakeWebhookOpener(),
    )

    status = await service.health_check()

    assert status.status is HealthState.DEGRADED
    assert status.message == "webhook health_url is not configured"


@pytest.mark.asyncio
async def test_notifications_plugin_registers_webhook_provider_when_configured():
    settings = InfraSettings(
        infra={
            "plugins": {
                "notifications": {
                    "enabled": True,
                    "config": {
                        "default_provider": "webhook",
                        "providers": {
                            "webhook": {
                                "url": "https://hooks.example.test/notify",
                                "health_url": "https://hooks.example.test/health",
                                "signing_secret": "hook-secret",
                            }
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[NotificationsPlugin()])

    await manager.startup()

    notifications = manager.get("notifications")
    assert isinstance(notifications, NotificationProviderRegistry)
    assert isinstance(notifications.get(), WebhookNotificationService)
    assert manager.health.snapshot()["notifications"].status is HealthState.DEGRADED

    await manager.shutdown()


@pytest.mark.asyncio
async def test_notifications_plugin_fails_when_smtp_config_is_missing():
    settings = InfraSettings(
        infra={
            "plugins": {
                "notifications": {
                    "enabled": True,
                    "config": {
                        "default_provider": "smtp",
                        "providers": {"smtp": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[NotificationsPlugin()])

    with pytest.raises(ValueError):
        await manager.startup()


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

    storage = manager.get("storage")
    assert isinstance(storage, StorageProviderRegistry)
    assert isinstance(storage.get(), LocalStorage)
    assert isinstance(manager.get("webhooks"), WebhookDispatcher)
    assert isinstance(manager.get("payment"), PaymentService)
    ratelimit = manager.get("ratelimit")
    assert isinstance(ratelimit, RateLimitBackendRegistry)
    assert isinstance(ratelimit.provider(), MemoryRateLimiter)
    notifications = manager.get("notifications")
    assert isinstance(notifications, NotificationProviderRegistry)
    assert isinstance(notifications.get(), NoopNotificationService)
    assert {name: status.status for name, status in manager.health.snapshot().items()} == {
        "storage": HealthState.HEALTHY,
        "webhooks": HealthState.HEALTHY,
        "payment": HealthState.HEALTHY,
        "ratelimit": HealthState.HEALTHY,
        "notifications": HealthState.DEGRADED,
    }
    assert (
        manager.health.snapshot()["notifications"].message
        == "noop notifications provider is enabled; messages are not delivered"
    )

    await manager.shutdown()
