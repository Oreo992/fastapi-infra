import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from infra.plugins.webhooks.models import WebhookEvent
from infra.plugins.webhooks.verification import ProviderSignatureVerifier, stripe_signature_verifier


class WebhookProviderError(ValueError):
    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class StripeWebhookProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    webhook_secret: str = Field(min_length=1)
    tolerance_seconds: int = 300


class JsonWebhookProvider:
    def __init__(
        self,
        name: str,
        *,
        verifier: ProviderSignatureVerifier | None = None,
    ) -> None:
        self.name = _normalize_provider_name(name)
        self._verifier = verifier

    def verify(self, payload: bytes, headers: Mapping[str, str]) -> bool:
        if self._verifier is None:
            return True
        return self._verifier(payload, headers)

    def build_event(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        decoded = self._decode_json(payload)
        return WebhookEvent(
            id=self._required_text(decoded, "id"),
            provider=self.name,
            type=self._required_text(decoded, "type"),
            payload=dict(decoded),
            headers=dict(headers),
        )

    def _decode_json(self, payload: bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookProviderError("bad_json", "webhook payload must be JSON") from exc
        if not isinstance(decoded, dict):
            raise WebhookProviderError("bad_json", "webhook payload must be a JSON object")
        return decoded

    def _required_text(self, payload: Mapping[str, Any], field: str) -> str:
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
        raise WebhookProviderError(
            "invalid_event",
            f"{self.name} webhook payload missing {field}",
        )


class StripeWebhookProvider(JsonWebhookProvider):
    name = "stripe"

    def __init__(self, config: StripeWebhookProviderConfig) -> None:
        super().__init__(
            self.name,
            verifier=stripe_signature_verifier(
                config.webhook_secret,
                tolerance_seconds=config.tolerance_seconds,
            ),
        )
        self.config = config


class WebhookProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}

    def register(self, provider: Any) -> None:
        self._providers[_normalize_provider_name(provider.name)] = provider

    def get(self, name: str) -> Any:
        provider_name = _normalize_provider_name(name)
        provider = self._providers.get(provider_name)
        if provider is None:
            raise LookupError(f"unknown webhook provider: {provider_name}")
        return provider

    def names(self) -> list[str]:
        return sorted(self._providers)


def _normalize_provider_name(provider: str) -> str:
    normalized = provider.strip().lower()
    if not normalized:
        raise ValueError("webhook provider must not be empty")
    return normalized
