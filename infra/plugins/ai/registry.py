from collections.abc import AsyncIterator

from infra.plugins.ai.models import ChatChunk, ChatMessage, ChatRequest, ChatResponse
from infra.plugins.ai.providers.base import AIProvider


class AIRegistry:
    def __init__(self, default_provider: str = "mock") -> None:
        self.default_provider = default_provider
        self._providers: dict[str, AIProvider] = {}

    def register(self, provider: AIProvider, *, default: bool = False) -> None:
        self._providers[provider.name] = provider
        if default:
            self.default_provider = provider.name

    def get(self, provider: str | None = None) -> AIProvider:
        name = provider or self.default_provider
        try:
            return self._providers[name]
        except KeyError as exc:
            raise LookupError(f"unknown ai provider: {name}") from exc

    async def chat(
        self,
        request: ChatRequest,
        *,
        provider: str | None = None,
    ) -> ChatResponse:
        return await self.get(provider).chat(request)

    def stream_chat(
        self,
        request: ChatRequest,
        *,
        provider: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        return self.get(provider).stream_chat(request)

    async def chat_text(
        self,
        text: str,
        *,
        model: str = "mock",
        provider: str | None = None,
    ) -> ChatResponse:
        return await self.chat(
            ChatRequest(
                model=model,
                messages=[ChatMessage(role="user", content=text)],
            ),
            provider=provider,
        )
