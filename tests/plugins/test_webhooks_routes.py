import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.config.models import InfraSettings
from infra.plugins.manager import PluginManager
from infra.plugins.payment import verify_webhook_signature
from infra.plugins.webhooks import (
    InMemoryWebhookStore,
    JsonWebhookProvider,
    SqlWebhookStore,
    WebhookDispatcher,
    WebhookEvent,
    WebhookProviderRegistry,
    WebhookSignatureVerifierRegistry,
    WebhooksPlugin,
    WebhooksPluginConfig,
    install_webhook_routes,
    stripe_signature_verifier,
)


def build_provider_registry(*providers):
    registry = WebhookProviderRegistry()
    for provider in providers or (JsonWebhookProvider("stripe"),):
        registry.register(provider)
    return registry


def build_client(provider_registry=None):
    app = FastAPI()
    dispatcher = WebhookDispatcher(
        provider_registry=provider_registry or build_provider_registry(),
    )
    dispatched = []

    async def handler(event_type, payload):
        dispatched.append((event_type, payload))

    dispatcher.register(handler)
    install_webhook_routes(
        app,
        dispatcher,
    )
    return TestClient(app), dispatched


def test_webhook_route_dispatches_valid_post_body():
    client, dispatched = build_client()

    response = client.post(
        "/webhooks/stripe",
        json={
            "id": "evt_123",
            "type": "checkout.session.completed",
            "data": {"id": "cs_123"},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "event_id": "evt_123"}
    assert dispatched == [
        (
            "checkout.session.completed",
            {
                "id": "evt_123",
                "type": "checkout.session.completed",
                "data": {"id": "cs_123"},
            },
        )
    ]


def test_webhook_route_deduplicates_by_provider_and_event_id():
    client, dispatched = build_client()
    payload = {"id": "evt_duplicate", "type": "payment.succeeded"}

    first = client.post("/webhooks/stripe", json=payload)
    second = client.post("/webhooks/stripe", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate", "event_id": "evt_duplicate"}
    assert dispatched == [("payment.succeeded", payload)]


def test_webhook_route_rejects_failed_signature_before_dispatch():
    def reject_signature(payload_bytes, headers):
        assert payload_bytes == b'{"id":"evt_bad","type":"charge.failed"}'
        assert "content-type" in headers
        return False

    client, dispatched = build_client(
        build_provider_registry(JsonWebhookProvider("stripe", verifier=reject_signature))
    )

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(
            {"id": "evt_bad", "type": "charge.failed"},
            separators=(",", ":"),
        ),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json() == {"status": "signature_failed"}
    assert dispatched == []


def test_webhook_route_rejects_bad_json_before_dispatch():
    client, dispatched = build_client()

    response = client.post(
        "/webhooks/stripe",
        content=b'{"id":',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"status": "bad_json", "detail": "webhook payload must be JSON"}
    assert dispatched == []


def test_webhook_route_enforces_declared_durable_store_requirement():
    app = FastAPI()
    dispatcher = WebhookDispatcher(durable_store_required=True)

    with pytest.raises(RuntimeError, match="durable WebhookStore"):
        install_webhook_routes(app, dispatcher)

    with pytest.raises(RuntimeError, match="durable WebhookStore"):
        install_webhook_routes(
            FastAPI(),
            dispatcher,
            store=InMemoryWebhookStore(),
        )


def test_webhook_route_enforces_declared_signature_requirement():
    app = FastAPI()
    dispatcher = WebhookDispatcher(required_providers={"stripe"})

    with pytest.raises(RuntimeError, match="missing providers"):
        install_webhook_routes(app, dispatcher)


def test_webhook_route_enforces_declared_provider_verifiers():
    app = FastAPI()
    dispatcher = WebhookDispatcher(
        provider_registry=build_provider_registry(JsonWebhookProvider("stripe")),
        required_providers={"stripe", "github"},
    )

    with pytest.raises(RuntimeError, match="github"):
        install_webhook_routes(app, dispatcher)


def test_webhook_route_accepts_declared_production_requirements():
    app = FastAPI()
    dispatcher = WebhookDispatcher(
        durable_store_required=True,
        provider_registry=build_provider_registry(JsonWebhookProvider("stripe")),
        required_providers={"stripe"},
    )

    store = install_webhook_routes(
        app,
        dispatcher,
        store=SqlWebhookStore(FakeWebhookDatabase()),
    )

    assert isinstance(store, SqlWebhookStore)


def test_webhook_route_rejects_unknown_provider():
    client, dispatched = build_client(build_provider_registry(JsonWebhookProvider("stripe")))

    response = client.post(
        "/webhooks/github",
        json={"id": "evt_123", "type": "push"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "unknown_provider"}
    assert dispatched == []


def test_webhook_signature_verifier_registry_registers_provider_specific_verifier():
    registry = WebhookSignatureVerifierRegistry()

    registry.register("Stripe", lambda payload, headers: payload == b"{}")

    assert registry.providers == frozenset({"stripe"})
    assert registry.verify("stripe", b"{}", {}) is True
    assert registry.verify("github", b"{}", {}) is False


def test_webhooks_config_normalizes_required_providers():
    config = WebhooksPluginConfig(required_providers=[" Stripe ", "stripe", "GitHub"])

    assert config.required_providers == ["stripe", "github"]


def test_webhooks_config_rejects_empty_verified_provider():
    with pytest.raises(ValueError, match="empty provider"):
        WebhooksPluginConfig(required_providers=["stripe", " "])


def test_stripe_signature_verifier_accepts_real_stripe_signature():
    payload = b'{"id":"evt_123","type":"payment.succeeded"}'
    secret = "whsec_test"
    timestamp = 1_700_000_000
    signature = _stripe_signature(payload, secret, timestamp)

    verifier = stripe_signature_verifier(secret, tolerance_seconds=-1)

    assert verifier(payload, {"stripe-signature": signature}) is True


def test_stripe_signature_verifier_rejects_missing_header():
    verifier = stripe_signature_verifier("whsec_test")

    assert verifier(b"{}", {}) is False


class DuplicateKeyError(Exception):
    pass


class FakeWebhookDatabase:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.statements: list[tuple[str, Any, bool]] = []

    async def execute_sql(self, sql: str, params: Any = None, commit: bool = True):
        self.statements.append((sql, params, commit))
        if sql.lstrip().upper().startswith("INSERT") and self.duplicate:
            raise DuplicateKeyError(1062, "Duplicate entry")
        return 1


class FakeWebhookEntryPoint:
    def __init__(self, name: str, loaded: object) -> None:
        self.name = name
        self.loaded = loaded

    def load(self) -> object:
        return self.loaded


class CustomWebhookProvider:
    name = "custom"

    def __init__(self, config):
        self.config = dict(config)

    def verify(self, payload, headers):
        return headers.get("x-custom-signature") == self.config["signature"]

    def build_event(self, payload, headers):
        decoded = json.loads(payload.decode("utf-8"))
        return WebhookEvent(
            id=decoded["event_id"],
            provider=self.name,
            type=decoded["event_type"],
            payload=decoded,
            headers=dict(headers),
        )


@pytest.mark.asyncio
async def test_webhooks_plugin_loads_external_provider_from_entry_point(monkeypatch):
    def provider_factory(config):
        return CustomWebhookProvider(config)

    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeWebhookEntryPoint("custom", provider_factory)],
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "webhooks": {
                    "enabled": True,
                    "config": {
                        "providers": {"custom": {"signature": "secret"}},
                        "required_providers": ["custom"],
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[WebhooksPlugin()])

    await manager.startup()

    dispatcher = manager.get("webhooks")
    provider = dispatcher.provider_registry.get("custom")
    assert isinstance(provider, CustomWebhookProvider)
    assert provider.config == {"signature": "secret"}
    assert provider.verify(b"{}", {"x-custom-signature": "secret"}) is True

    await manager.shutdown()


async def test_sql_webhook_store_records_event_once():
    database = FakeWebhookDatabase()
    store = SqlWebhookStore(database)
    event = WebhookEvent(
        id="evt_sql",
        provider="stripe",
        type="payment.succeeded",
        payload={"id": "evt_sql", "type": "payment.succeeded"},
        headers={"stripe-signature": "sig"},
    )

    recorded = await store.record_once(event)

    assert recorded is True
    assert len(database.statements) == 2
    assert "CREATE TABLE IF NOT EXISTS infra_webhook_events" in database.statements[0][0]
    assert "INSERT INTO infra_webhook_events" in database.statements[1][0]
    assert database.statements[1][1][0:3] == (
        "stripe",
        "evt_sql",
        "payment.succeeded",
    )


async def test_sql_webhook_store_treats_duplicate_key_as_seen():
    database = FakeWebhookDatabase(duplicate=True)
    store = SqlWebhookStore(database)
    event = WebhookEvent(
        id="evt_seen",
        provider="stripe",
        type="payment.succeeded",
        payload={"id": "evt_seen", "type": "payment.succeeded"},
    )

    recorded = await store.record_once(event)

    assert recorded is False


def test_sql_webhook_store_rejects_unsafe_table_name():
    import pytest

    with pytest.raises(ValueError, match="table_name"):
        SqlWebhookStore(FakeWebhookDatabase(), "events; DROP TABLE users")


def _stripe_signature(payload: bytes, secret: str, timestamp: int) -> str:
    import hashlib
    import hmac

    signed_payload = str(timestamp).encode() + b"." + payload
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(
        payload,
        f"t={timestamp},v1={digest}",
        secret,
        tolerance_seconds=-1,
    )
    return f"t={timestamp},v1={digest}"
