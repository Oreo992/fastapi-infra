from collections.abc import Callable, Mapping

from infra.plugins.webhooks.models import normalize_webhook_provider_name

ProviderSignatureVerifier = Callable[[bytes, Mapping[str, str]], bool]


class WebhookSignatureVerifierRegistry:
    def __init__(
        self,
        verifiers: Mapping[str, ProviderSignatureVerifier] | None = None,
    ) -> None:
        self._verifiers: dict[str, ProviderSignatureVerifier] = {}
        for provider, verifier in (verifiers or {}).items():
            self.register(provider, verifier)

    @property
    def providers(self) -> frozenset[str]:
        return frozenset(self._verifiers)

    def register(
        self,
        provider: str,
        verifier: ProviderSignatureVerifier,
    ) -> ProviderSignatureVerifier:
        provider_name = normalize_webhook_provider_name(provider)
        if not callable(verifier):
            raise TypeError("webhook signature verifier must be callable")
        self._verifiers[provider_name] = verifier
        return verifier

    def verify(self, provider: str, payload: bytes, headers: Mapping[str, str]) -> bool:
        verifier = self._verifiers.get(normalize_webhook_provider_name(provider))
        if verifier is None:
            return False
        return verifier(payload, headers)


def stripe_signature_verifier(
    webhook_secret: str,
    *,
    header_name: str = "stripe-signature",
    tolerance_seconds: int = 300,
) -> ProviderSignatureVerifier:
    from infra.plugins.payment import verify_webhook_signature

    def verify(payload: bytes, headers: Mapping[str, str]) -> bool:
        signature = headers.get(header_name)
        if signature is None:
            return False
        return verify_webhook_signature(
            payload,
            signature,
            webhook_secret,
            tolerance_seconds=tolerance_seconds,
        )

    return verify


def build_signature_verifier_registry(
    verifiers: WebhookSignatureVerifierRegistry | Mapping[str, ProviderSignatureVerifier] | None,
) -> WebhookSignatureVerifierRegistry | None:
    if verifiers is None:
        return None
    if isinstance(verifiers, WebhookSignatureVerifierRegistry):
        return verifiers
    return WebhookSignatureVerifierRegistry(verifiers)
