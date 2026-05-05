from collections.abc import AsyncIterator
from typing import Any

from infra.plugins.ai.adapters._shared import (
    definition_dicts,
    iter_any,
    maybe_await,
    message_dicts,
    text_from,
)
from infra.plugins.ai.models import ChatChunk, ChatRequest, ChatResponse


class OpenAIProvider:
    name = "openai"

    def __init__(self, client: Any = None) -> None:
        self._client = client

    async def chat(self, request: ChatRequest) -> ChatResponse:
        response = await maybe_await(
            self._get_client().responses.create(**self._kwargs(request))
        )
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=text_from(response),
            raw=response,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        stream = self._get_client().responses.create(
            **self._kwargs(request),
            stream=True,
        )
        async for item in iter_any(stream):
            yield ChatChunk(
                provider=self.name,
                model=request.model,
                content=text_from(item),
            )

    def _kwargs(self, request: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "input": message_dicts(request.messages),
        }
        if request.tools:
            kwargs["tools"] = definition_dicts(request)
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_output_tokens"] = request.max_tokens
        return kwargs

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "openai SDK is not installed; install openai or pass a client"
            ) from exc
        self._client = AsyncOpenAI()
        return self._client
