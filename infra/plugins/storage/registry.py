from typing import Any


class StorageProviderRegistry:
    def __init__(self, default_provider: str = "local") -> None:
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
            raise LookupError(f"unknown storage provider: {provider_name}")
        return provider

    async def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        *,
        provider: str | None = None,
    ) -> None:
        await self.get(provider).put_object(key, data, content_type, metadata)

    async def get_object(self, key: str, *, provider: str | None = None) -> bytes:
        data = await self.get(provider).get_object(key)
        return bytes(data)

    async def exists(self, key: str, *, provider: str | None = None) -> bool:
        return bool(await self.get(provider).exists(key))

    async def delete_object(self, key: str, *, provider: str | None = None) -> None:
        await self.get(provider).delete_object(key)

    async def list_objects(self, prefix: str = "", *, provider: str | None = None) -> list[str]:
        return list(await self.get(provider).list_objects(prefix))

    def presign_get_url(
        self,
        key: str,
        expires_seconds: int = 3600,
        *,
        provider: str | None = None,
    ) -> str:
        return str(self.get(provider).presign_get_url(key, expires_seconds))

    def names(self) -> list[str]:
        return sorted(self._providers)
