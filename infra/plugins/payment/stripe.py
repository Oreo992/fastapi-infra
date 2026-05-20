import asyncio
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from typing import ClassVar, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from infra.core.health import HealthState, HealthStatus
from infra.plugins.payment.models import PaymentCheckout, PaymentRefund
from infra.plugins.retry import retry_provider_operation


class StripeTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> tuple[int, bytes]:
        raise NotImplementedError


class UrllibStripeTransport:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> tuple[int, bytes]:
        return await asyncio.to_thread(self._request, method, url, headers, data)

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


class StripeProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    DEFAULT_API_BASE: ClassVar[str] = "https://api.stripe.com"

    api_key: str = Field(min_length=1, repr=False)
    webhook_secret: str | None = Field(default=None, repr=False)
    api_base: str = DEFAULT_API_BASE
    timeout: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    retry_base_delay: float = Field(default=0.25, ge=0)

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("api_base must be an absolute http(s) URL")
        return value.rstrip("/")

    @field_validator("webhook_secret")
    @classmethod
    def normalize_webhook_secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value or None

    @field_validator("timeout", "max_attempts", "retry_base_delay", mode="before")
    @classmethod
    def reject_bool_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value


class StripeAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class StripePaymentProvider:
    name = "stripe"
    retry_status_codes = frozenset({409, 429, 500, 502, 503, 504})

    def __init__(
        self,
        config: StripeProviderConfig,
        transport: StripeTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibStripeTransport(timeout=config.timeout)

    async def health_check(self) -> HealthStatus:
        try:
            response = await self._request("GET", "/v1/account", None)
        except Exception as exc:
            return HealthStatus(
                name=self.name,
                status=HealthState.UNHEALTHY,
                message=str(exc) or exc.__class__.__name__,
                details={"provider": self.name},
            )
        details: dict[str, object] = {"provider": self.name}
        account_id = response.get("id")
        if isinstance(account_id, str) and account_id:
            details["account_id"] = account_id
        return HealthStatus(name=self.name, status=HealthState.HEALTHY, details=details)

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
        if amount <= 0:
            raise ValueError("amount must be positive")
        if not success_url:
            raise ValueError("stripe checkout requires success_url")
        if not cancel_url:
            raise ValueError("stripe checkout requires cancel_url")

        form: dict[str, object] = {
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][quantity]": 1,
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": amount,
            "line_items[0][price_data][product_data][name]": reference or "Checkout",
        }
        if reference is not None:
            form["client_reference_id"] = reference
        for key, value in (metadata or {}).items():
            form[f"metadata[{key}]"] = value
        options: dict[str, object] = provider_options or {}
        idempotency_key = self._idempotency_key(
            options,
            default_reference=reference,
            operation="checkout",
        )
        for option_key, option_value in options.items():
            if option_key == "idempotency_key":
                continue
            form[option_key] = option_value

        payload = urllib.parse.urlencode(form, doseq=True).encode()
        response = await self._request(
            "POST",
            "/v1/checkout/sessions",
            payload,
            idempotency_key=idempotency_key,
        )
        return self._checkout_from_response(response)

    async def get_checkout(self, checkout_id: str) -> PaymentCheckout:
        quoted_id = urllib.parse.quote(checkout_id, safe="")
        response = await self._request(
            "GET",
            f"/v1/checkout/sessions/{quoted_id}",
            None,
        )
        return self._checkout_from_response(response)

    async def get_payment_status(self, checkout_id: str) -> str:
        checkout = await self.get_checkout(checkout_id)
        return checkout.status

    async def create_refund(
        self,
        checkout_id: str,
        amount: int,
        currency: str,
        reference: str | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentRefund:
        if amount <= 0:
            raise ValueError("amount must be positive")

        options: dict[str, object] = provider_options or {}
        idempotency_key = self._idempotency_key(
            options,
            default_reference=reference,
            operation="refund",
        )
        payment_intent = self._string_option(options, "payment_intent")
        charge = self._string_option(options, "charge")
        if payment_intent and charge:
            raise ValueError("stripe refund accepts payment_intent or charge, not both")

        if payment_intent is None and charge is None:
            session = await self._get_checkout_session_response(checkout_id)
            payment_intent = self._response_string(session, "payment_intent")
            if payment_intent is None:
                charge = self._response_string(session, "charge")
        if payment_intent is None and charge is None:
            raise ValueError(
                "stripe refund requires payment_intent or charge; checkout session did not include either"
            )

        form: dict[str, object] = {"amount": amount}
        if payment_intent is not None:
            form["payment_intent"] = payment_intent
        if charge is not None:
            form["charge"] = charge
        if reference is not None:
            form["metadata[reference]"] = reference
        for option_key, option_value in options.items():
            if option_key in {"idempotency_key", "payment_intent", "charge"}:
                continue
            form[option_key] = option_value

        payload = urllib.parse.urlencode(form, doseq=True).encode()
        response = await self._request(
            "POST",
            "/v1/refunds",
            payload,
            idempotency_key=idempotency_key,
        )
        return self._refund_from_response(
            response,
            checkout_id=checkout_id,
            requested_currency=currency,
            reference=reference,
        )

    async def _get_checkout_session_response(
        self,
        checkout_id: str,
    ) -> dict[str, object]:
        quoted_id = urllib.parse.quote(checkout_id, safe="")
        return await self._request(
            "GET",
            f"/v1/checkout/sessions/{quoted_id}",
            None,
        )

    async def _request(
        self,
        method: str,
        path: str,
        data: bytes | None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        return await retry_provider_operation(
            lambda: self._request_once(
                method,
                path,
                data,
                idempotency_key=idempotency_key,
            ),
            max_attempts=self.config.max_attempts,
            base_delay=self.config.retry_base_delay,
            is_retryable_exception=lambda exc: isinstance(exc, StripeAPIError) and exc.retryable,
            exhausted_message="stripe max_attempts must allow at least one request",
        )

    async def _request_once(
        self,
        method: str,
        path: str,
        data: bytes | None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Stripe-Version": "2024-06-20",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        url = f"{self.config.api_base}{path}"
        try:
            status, body = await self.transport.request(method, url, headers, data)
        except Exception as exc:
            raise StripeAPIError(
                f"stripe transport error: {exc}",
                retryable=True,
            ) from exc
        retryable = status in self.retry_status_codes
        try:
            decoded = json.loads(body.decode() or "{}")
        except json.JSONDecodeError as exc:
            raise StripeAPIError(
                "stripe returned invalid JSON",
                status_code=status,
                retryable=retryable,
            ) from exc

        if status >= 400:
            message = "stripe API request failed"
            if isinstance(decoded, dict):
                error = decoded.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    message = error["message"]
            raise StripeAPIError(
                message,
                status_code=status,
                retryable=retryable,
            )
        if not isinstance(decoded, dict):
            raise RuntimeError("stripe returned an unexpected response")
        return decoded

    def _checkout_from_response(self, response: Mapping[str, object]) -> PaymentCheckout:
        checkout_id = response.get("id")
        if not isinstance(checkout_id, str):
            raise RuntimeError("stripe checkout response missing id")

        amount = response.get("amount_total")
        if amount is None:
            amount = response.get("amount_subtotal")
        if not isinstance(amount, int):
            amount = 0

        currency = response.get("currency")
        if not isinstance(currency, str):
            currency = ""

        reference = response.get("client_reference_id")
        if not isinstance(reference, str):
            reference = None

        url = response.get("url")
        if not isinstance(url, str):
            url = ""

        return PaymentCheckout(
            id=checkout_id,
            amount=amount,
            currency=currency.upper(),
            reference=reference,
            status=self._map_status(response),
            url=url,
        )

    def _map_status(self, response: Mapping[str, object]) -> str:
        payment_status = response.get("payment_status")
        session_status = response.get("status")

        if payment_status == "paid":
            return "paid"
        if session_status == "complete":
            return "paid"
        if session_status == "expired":
            return "expired"
        if payment_status == "unpaid" or session_status == "open":
            return "pending"
        if isinstance(payment_status, str):
            return payment_status
        if isinstance(session_status, str):
            return session_status
        return "unknown"

    def _refund_from_response(
        self,
        response: Mapping[str, object],
        *,
        checkout_id: str,
        requested_currency: str,
        reference: str | None,
    ) -> PaymentRefund:
        refund_id = response.get("id")
        if not isinstance(refund_id, str):
            raise RuntimeError("stripe refund response missing id")

        amount = response.get("amount")
        if not isinstance(amount, int):
            amount = 0

        currency = response.get("currency")
        if not isinstance(currency, str):
            currency = requested_currency

        status = response.get("status")
        if not isinstance(status, str):
            status = "unknown"

        if reference is None:
            metadata = response.get("metadata")
            if isinstance(metadata, Mapping):
                metadata_reference = metadata.get("reference")
                if isinstance(metadata_reference, str):
                    reference = metadata_reference

        return PaymentRefund(
            id=refund_id,
            checkout_id=checkout_id,
            amount=amount,
            currency=currency.upper(),
            status=status,
            reference=reference,
        )

    def _idempotency_key(
        self,
        provider_options: Mapping[str, object],
        *,
        default_reference: str | None = None,
        operation: str,
    ) -> str | None:
        idempotency_key = provider_options.get("idempotency_key")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key:
                raise ValueError("stripe idempotency_key must be a non-empty string")
            return idempotency_key
        if default_reference is None:
            return f"fastapi-infra:{operation}:{uuid.uuid4().hex}"
        digest = hashlib.sha256(f"{operation}:{default_reference}".encode()).hexdigest()
        return f"fastapi-infra:{operation}:{digest}"

    def _string_option(
        self,
        provider_options: Mapping[str, object],
        key: str,
    ) -> str | None:
        value = provider_options.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError(f"stripe {key} must be a non-empty string")
        return value

    def _response_string(
        self,
        response: Mapping[str, object],
        key: str,
    ) -> str | None:
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
        return None

    def verify_webhook_signature(
        self,
        payload: bytes,
        header: str,
        tolerance_seconds: int = 300,
        now: int | float | None = None,
    ) -> bool:
        if self.config.webhook_secret is None:
            raise ValueError("stripe webhook_secret is not configured")
        return verify_webhook_signature(
            payload=payload,
            header=header,
            secret=self.config.webhook_secret,
            tolerance_seconds=tolerance_seconds,
            now=now,
        )


def verify_webhook_signature(
    payload: bytes,
    header: str,
    secret: str,
    tolerance_seconds: int = 300,
    now: int | float | None = None,
) -> bool:
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, separator, value = part.partition("=")
        if separator != "=":
            continue
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return False
        elif key == "v1":
            signatures.append(value)

    if timestamp is None or not signatures:
        return False

    current_time = time.time() if now is None else now
    if tolerance_seconds >= 0 and abs(current_time - timestamp) > tolerance_seconds:
        return False

    signed_payload = str(timestamp).encode() + b"." + payload
    expected = hmac.new(
        secret.encode(),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(expected, signature) for signature in signatures)
