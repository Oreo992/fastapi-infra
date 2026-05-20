import hashlib
import hmac
import json
import urllib.parse

import pytest
from pydantic import ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.manager import PluginManager
from infra.plugins.payment import (
    PaymentPlugin,
    StripeAPIError,
    StripePaymentProvider,
    StripeProviderConfig,
    verify_webhook_signature,
)


class FakeStripeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, url, headers, data):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "data": data,
            }
        )
        status, payload = self.responses.pop(0)
        return status, json.dumps(payload).encode()


def test_urllib_stripe_transport_uses_configured_timeout(monkeypatch):
    import infra.plugins.payment.stripe as stripe_module

    calls = []

    class FakeHTTPResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        calls.append({"request": request, "timeout": timeout})
        return FakeHTTPResponse()

    monkeypatch.setattr(stripe_module.urllib.request, "urlopen", fake_urlopen)
    transport = stripe_module.UrllibStripeTransport(timeout=6.5)

    status, body = transport._request("GET", "https://stripe.test/v1/customers", {}, None)

    assert status == 200
    assert body == b"{}"
    assert calls[0]["timeout"] == 6.5


def test_stripe_provider_config_accepts_positive_timeout():
    config = StripeProviderConfig.model_validate(
        {
            "api_key": "sk-test",
            "api_base": "https://stripe.test",
            "timeout": 4.5,
            "max_attempts": 2,
            "retry_base_delay": 0,
        }
    )

    assert config.timeout == 4.5
    assert config.max_attempts == 2
    assert config.retry_base_delay == 0


def test_stripe_provider_config_rejects_invalid_retry_options():
    with pytest.raises(ValidationError, match="max_attempts"):
        StripeProviderConfig.model_validate({"api_key": "sk-test", "max_attempts": 0})
    with pytest.raises(ValidationError, match="max_attempts"):
        StripeProviderConfig.model_validate({"api_key": "sk-test", "max_attempts": True})
    with pytest.raises(ValidationError, match="retry_base_delay"):
        StripeProviderConfig.model_validate({"api_key": "sk-test", "retry_base_delay": -0.1})
    with pytest.raises(ValidationError, match="timeout"):
        StripeProviderConfig.model_validate({"api_key": "sk-test", "timeout": True})


def test_stripe_provider_config_rejects_blank_api_key():
    with pytest.raises(ValidationError, match="api_key"):
        StripeProviderConfig.model_validate({"api_key": "   "})


def test_stripe_provider_config_rejects_invalid_api_base():
    with pytest.raises(ValidationError, match="api_base"):
        StripeProviderConfig.model_validate({"api_key": "sk-test", "api_base": "stripe.test"})


@pytest.mark.asyncio
async def test_stripe_health_check_probes_account_endpoint():
    transport = FakeStripeTransport([(200, {"id": "acct_test_123"})])
    provider = StripePaymentProvider(
        StripeProviderConfig(api_key="sk_test_123", api_base="https://stripe.test"),
        transport=transport,
    )

    status = await provider.health_check()

    request = transport.requests[0]
    assert status.status is HealthState.HEALTHY
    assert status.details == {"provider": "stripe", "account_id": "acct_test_123"}
    assert request["method"] == "GET"
    assert request["url"] == "https://stripe.test/v1/account"
    assert request["headers"]["Authorization"] == "Bearer sk_test_123"
    assert request["data"] is None


@pytest.mark.asyncio
async def test_stripe_health_check_reports_upstream_failure():
    transport = FakeStripeTransport([(401, {"error": {"message": "invalid api key"}})])
    provider = StripePaymentProvider(
        StripeProviderConfig(api_key="sk_test_123", api_base="https://stripe.test"),
        transport=transport,
    )

    status = await provider.health_check()

    assert status.status is HealthState.UNHEALTHY
    assert status.message == "invalid api key"
    assert status.details == {"provider": "stripe"}


@pytest.mark.asyncio
async def test_stripe_retries_retryable_status_before_succeeding():
    transport = FakeStripeTransport(
        [
            (429, {"error": {"message": "rate limited"}}),
            (
                200,
                {
                    "id": "cs_test_retry",
                    "amount_total": 1250,
                    "currency": "usd",
                    "payment_status": "unpaid",
                    "status": "open",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_retry",
                },
            ),
        ]
    )
    provider = StripePaymentProvider(
        StripeProviderConfig(
            api_key="sk_test_123",
            api_base="https://stripe.test",
            max_attempts=2,
            retry_base_delay=0,
        ),
        transport=transport,
    )

    checkout = await provider.create_checkout(
        amount=1250,
        currency="USD",
        success_url="https://example.test/success",
        cancel_url="https://example.test/cancel",
    )

    assert checkout.id == "cs_test_retry"
    assert len(transport.requests) == 2
    assert transport.requests[0]["headers"]["Idempotency-Key"].startswith("fastapi-infra:checkout:")
    assert (
        transport.requests[0]["headers"]["Idempotency-Key"]
        == transport.requests[1]["headers"]["Idempotency-Key"]
    )


@pytest.mark.asyncio
async def test_stripe_does_not_retry_non_retryable_api_errors():
    transport = FakeStripeTransport(
        [(400, {"error": {"message": "bad request"}}), (200, {"id": "unexpected"})]
    )
    provider = StripePaymentProvider(
        StripeProviderConfig(
            api_key="sk_test_123",
            api_base="https://stripe.test",
            max_attempts=2,
            retry_base_delay=0,
        ),
        transport=transport,
    )

    with pytest.raises(StripeAPIError) as exc:
        await provider.get_checkout("cs_test_bad")

    assert exc.value.status_code == 400
    assert exc.value.retryable is False
    assert str(exc.value) == "bad request"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_stripe_create_checkout_constructs_real_checkout_session_request():
    transport = FakeStripeTransport(
        [
            (
                200,
                {
                    "id": "cs_test_123",
                    "amount_total": 1250,
                    "currency": "usd",
                    "client_reference_id": "order-123",
                    "payment_status": "unpaid",
                    "status": "open",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_123",
                },
            )
        ]
    )
    provider = StripePaymentProvider(
        StripeProviderConfig(api_key="sk_test_123", api_base="https://stripe.test"),
        transport=transport,
    )

    checkout = await provider.create_checkout(
        amount=1250,
        currency="USD",
        reference="order-123",
        success_url="https://example.test/success",
        cancel_url="https://example.test/cancel",
        metadata={"tenant": "acme"},
        provider_options={
            "customer_email": "buyer@example.test",
            "idempotency_key": "checkout-order-123",
        },
    )

    request = transport.requests[0]
    body = urllib.parse.parse_qs(request["data"].decode())

    assert request["method"] == "POST"
    assert request["url"] == "https://stripe.test/v1/checkout/sessions"
    assert request["headers"]["Authorization"] == "Bearer sk_test_123"
    assert request["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert request["headers"]["Idempotency-Key"] == "checkout-order-123"
    assert body["mode"] == ["payment"]
    assert body["success_url"] == ["https://example.test/success"]
    assert body["cancel_url"] == ["https://example.test/cancel"]
    assert body["client_reference_id"] == ["order-123"]
    assert body["line_items[0][price_data][unit_amount]"] == ["1250"]
    assert body["line_items[0][price_data][currency]"] == ["usd"]
    assert body["metadata[tenant]"] == ["acme"]
    assert body["customer_email"] == ["buyer@example.test"]
    assert "idempotency_key" not in body
    assert checkout.id == "cs_test_123"
    assert checkout.amount == 1250
    assert checkout.currency == "USD"
    assert checkout.reference == "order-123"
    assert checkout.status == "pending"
    assert checkout.url.endswith("cs_test_123")


@pytest.mark.asyncio
async def test_stripe_create_checkout_derives_idempotency_key_from_reference():
    transport = FakeStripeTransport(
        [
            (
                200,
                {
                    "id": "cs_test_derived",
                    "amount_total": 1250,
                    "currency": "usd",
                    "client_reference_id": "order-123",
                    "payment_status": "unpaid",
                    "status": "open",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_derived",
                },
            )
        ]
    )
    provider = StripePaymentProvider(
        StripeProviderConfig(api_key="sk_test_123", api_base="https://stripe.test"),
        transport=transport,
    )

    await provider.create_checkout(
        amount=1250,
        currency="USD",
        reference="order-123",
        success_url="https://example.test/success",
        cancel_url="https://example.test/cancel",
    )

    expected = hashlib.sha256(b"checkout:order-123").hexdigest()
    assert (
        transport.requests[0]["headers"]["Idempotency-Key"] == f"fastapi-infra:checkout:{expected}"
    )


@pytest.mark.asyncio
async def test_stripe_create_refund_uses_payment_intent_and_idempotency_key():
    transport = FakeStripeTransport(
        [
            (
                200,
                {
                    "id": "re_test_123",
                    "amount": 750,
                    "currency": "usd",
                    "status": "succeeded",
                    "payment_intent": "pi_test_123",
                    "metadata": {"reference": "refund-order-123"},
                },
            )
        ]
    )
    provider = StripePaymentProvider(
        StripeProviderConfig(api_key="sk_test_123", api_base="https://stripe.test"),
        transport=transport,
    )

    refund = await provider.create_refund(
        checkout_id="cs_test_123",
        amount=750,
        currency="USD",
        provider_options={
            "payment_intent": "pi_test_123",
            "idempotency_key": "refund-order-123",
            "reason": "requested_by_customer",
        },
    )

    request = transport.requests[0]
    body = urllib.parse.parse_qs(request["data"].decode())

    assert request["method"] == "POST"
    assert request["url"] == "https://stripe.test/v1/refunds"
    assert request["headers"]["Idempotency-Key"] == "refund-order-123"
    assert body["amount"] == ["750"]
    assert body["payment_intent"] == ["pi_test_123"]
    assert body["reason"] == ["requested_by_customer"]
    assert "idempotency_key" not in body
    assert refund.id == "re_test_123"
    assert refund.checkout_id == "cs_test_123"
    assert refund.amount == 750
    assert refund.currency == "USD"
    assert refund.status == "succeeded"
    assert refund.reference == "refund-order-123"


@pytest.mark.asyncio
async def test_stripe_create_refund_can_use_charge():
    transport = FakeStripeTransport(
        [
            (
                200,
                {
                    "id": "re_test_charge",
                    "amount": 500,
                    "currency": "eur",
                    "status": "pending",
                    "charge": "ch_test_123",
                },
            )
        ]
    )
    provider = StripePaymentProvider(
        StripeProviderConfig(api_key="sk_test_123", api_base="https://stripe.test"),
        transport=transport,
    )

    refund = await provider.create_refund(
        checkout_id="cs_test_123",
        amount=500,
        currency="EUR",
        reference="refund-charge",
        provider_options={"charge": "ch_test_123"},
    )

    request = transport.requests[0]
    body = urllib.parse.parse_qs(request["data"].decode())

    assert request["url"] == "https://stripe.test/v1/refunds"
    expected = hashlib.sha256(b"refund:refund-charge").hexdigest()
    assert request["headers"]["Idempotency-Key"] == f"fastapi-infra:refund:{expected}"
    assert body["charge"] == ["ch_test_123"]
    assert body["metadata[reference]"] == ["refund-charge"]
    assert refund.id == "re_test_charge"
    assert refund.currency == "EUR"
    assert refund.reference == "refund-charge"


@pytest.mark.asyncio
async def test_stripe_create_refund_fetches_checkout_session_payment_intent():
    transport = FakeStripeTransport(
        [
            (
                200,
                {
                    "id": "cs_test_123",
                    "amount_total": 1250,
                    "currency": "usd",
                    "payment_intent": "pi_from_checkout",
                    "payment_status": "paid",
                    "status": "complete",
                },
            ),
            (
                200,
                {
                    "id": "re_test_123",
                    "amount": 1250,
                    "currency": "usd",
                    "status": "succeeded",
                    "payment_intent": "pi_from_checkout",
                },
            ),
        ]
    )
    provider = StripePaymentProvider(
        StripeProviderConfig(api_key="sk_test_123", api_base="https://stripe.test"),
        transport=transport,
    )

    refund = await provider.create_refund(
        checkout_id="cs_test_123",
        amount=1250,
        currency="USD",
    )

    lookup_request = transport.requests[0]
    refund_request = transport.requests[1]
    body = urllib.parse.parse_qs(refund_request["data"].decode())

    assert lookup_request["method"] == "GET"
    assert lookup_request["url"] == "https://stripe.test/v1/checkout/sessions/cs_test_123"
    assert refund_request["method"] == "POST"
    assert refund_request["url"] == "https://stripe.test/v1/refunds"
    assert body["payment_intent"] == ["pi_from_checkout"]
    assert refund.status == "succeeded"


@pytest.mark.asyncio
async def test_stripe_create_refund_errors_without_payment_intent_or_charge():
    transport = FakeStripeTransport(
        [
            (
                200,
                {
                    "id": "cs_test_123",
                    "amount_total": 1250,
                    "currency": "usd",
                    "payment_status": "paid",
                    "status": "complete",
                },
            )
        ]
    )
    provider = StripePaymentProvider(
        StripeProviderConfig(api_key="sk_test_123", api_base="https://stripe.test"),
        transport=transport,
    )

    with pytest.raises(ValueError, match="requires payment_intent or charge"):
        await provider.create_refund(
            checkout_id="cs_test_123",
            amount=1250,
            currency="USD",
        )

    assert len(transport.requests) == 1
    assert transport.requests[0]["url"] == ("https://stripe.test/v1/checkout/sessions/cs_test_123")


@pytest.mark.asyncio
async def test_stripe_get_checkout_maps_paid_status():
    transport = FakeStripeTransport(
        [
            (
                200,
                {
                    "id": "cs_test_paid",
                    "amount_total": 5000,
                    "currency": "eur",
                    "client_reference_id": "order-paid",
                    "payment_status": "paid",
                    "status": "complete",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_paid",
                },
            )
        ]
    )
    provider = StripePaymentProvider(
        StripeProviderConfig(api_key="sk_test_123", api_base="https://stripe.test"),
        transport=transport,
    )

    checkout = await provider.get_checkout("cs_test_paid")

    request = transport.requests[0]
    assert request["method"] == "GET"
    assert request["url"] == "https://stripe.test/v1/checkout/sessions/cs_test_paid"
    assert request["data"] is None
    assert checkout.amount == 5000
    assert checkout.currency == "EUR"
    assert checkout.reference == "order-paid"
    assert checkout.status == "paid"


def test_stripe_verify_webhook_signature_accepts_valid_header():
    payload = b'{"id":"evt_123"}'
    secret = "whsec_test"
    timestamp = 1_700_000_000
    signature = hmac.new(
        secret.encode(),
        str(timestamp).encode() + b"." + payload,
        hashlib.sha256,
    ).hexdigest()

    assert verify_webhook_signature(
        payload,
        f"t={timestamp},v1={signature}",
        secret,
        now=timestamp,
    )


def test_stripe_verify_webhook_signature_rejects_bad_signature():
    assert not verify_webhook_signature(
        b'{"id":"evt_123"}',
        "t=1700000000,v1=bad",
        "whsec_test",
        now=1_700_000_000,
    )


@pytest.mark.asyncio
async def test_payment_plugin_fails_when_stripe_enabled_without_api_key():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "providers": {"stripe": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[PaymentPlugin()])

    with pytest.raises(ValidationError, match="api_key"):
        await manager.startup()
