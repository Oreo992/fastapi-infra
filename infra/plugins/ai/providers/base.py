from collections.abc import AsyncIterator
from typing import Protocol

from infra.plugins.ai.models import ChatChunk, ChatRequest, ChatResponse


class AIProvider(Protocol):
    @property
    def name(self) -> str:
        raise NotImplementedError

    async def chat(self, request: ChatRequest) -> ChatResponse:
        raise NotImplementedError

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError
