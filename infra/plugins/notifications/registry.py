from typing import Any


class NotificationProviderRegistry:
    def __init__(self, default_provider: str = "noop") -> None:
        self.default_provider = default_provider
        self._providers: dict[str, Any] = {}

    def register(self, provider: Any, *, default: bool = False) -> None:
        self._providers[provider.name] = provider
        if default:
            self.default_provider = provider.name

    def get(self, name: str | None = None) -> Any:
        provider_name = name or self.default_provider
        provider = self._providers.get(provider_name)
        if provider is None:
            raise LookupError(f"unknown notification provider: {provider_name}")
        return provider

    async def send(
        self,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
        *,
        provider: str | None = None,
    ) -> Any:
        return await self.get(provider).send(channel, recipient, subject, body, metadata)

    def names(self) -> list[str]:
        return sorted(self._providers)
