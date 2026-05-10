from infra.plugins.payment.providers import PaymentProvider


class PaymentProviderRegistry:
    def __init__(self, default_provider: str = "mock") -> None:
        self.default_provider = default_provider
        self._providers: dict[str, PaymentProvider] = {}

    def register(self, provider: PaymentProvider, *, default: bool = False) -> None:
        self._providers[provider.name] = provider
        if default:
            self.default_provider = provider.name

    def get(self, name: str | None = None) -> PaymentProvider:
        provider_name = name or self.default_provider
        provider = self._providers.get(provider_name)
        if provider is None:
            raise LookupError(f"unknown payment provider: {provider_name}")
        return provider

    def names(self) -> list[str]:
        return sorted(self._providers)
