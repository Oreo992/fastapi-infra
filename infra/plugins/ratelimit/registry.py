from typing import Any


class RateLimitBackendRegistry:
    def __init__(self, default_provider: str = "memory") -> None:
        self.default_provider = default_provider
        self._providers: dict[str, Any] = {}

    def register(self, provider: Any, *, default: bool = False) -> None:
        self._providers[provider.name] = provider
        if default:
            self.default_provider = provider.name

    def provider(self, name: str | None = None) -> Any:
        provider_name = name or self.default_provider
        provider = self._providers.get(provider_name)
        if provider is None:
            raise LookupError(f"unknown rate limit provider: {provider_name}")
        return provider

    async def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        return bool(await self.provider().allow(key, limit, window_seconds))

    def names(self) -> list[str]:
        return sorted(self._providers)
