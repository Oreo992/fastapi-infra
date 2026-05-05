from collections.abc import AsyncIterator
from typing import Any

from infra.plugins.ai.adapters._shared import (
    definition_dicts,
    iter_any,
    maybe_await,
    message_dicts,
    text_from,
    tool_calls_from,
)
from infra.plugins.ai.models import ChatChunk, ChatRequest, ChatResponse


class AnthropicAIProvider:
    name = "anthropic"

    def __init__(self, client: Any = None) -> None:
        self._client = client

    async def chat(self, request: ChatRequest) -> ChatResponse:
        response = await maybe_await(
            self._get_client().messages.create(**self._kwargs(request))
        )
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=text_from(response),
            tool_calls=tool_calls_from(response),
            raw=response,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        async with self._get_client().messages.stream(**self._kwargs(request)) as stream:
            text_stream = stream.text_stream
            chunks = text_stream() if callable(text_stream) else text_stream
            async for text in chunks:
                yield ChatChunk(provider=self.name, model=request.model, content=text)
            if hasattr(stream, "__aiter__"):
                async for item in iter_any(stream):
                    tool_calls = tool_calls_from(item)
                    if tool_calls:
                        yield ChatChunk(
                            provider=self.name,
                            model=request.model,
                            tool_calls=tool_calls,
                        )

    def _kwargs(self, request: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [
                item for item in message_dicts(request.messages) if item["role"] != "system"
            ],
            "max_tokens": request.max_tokens or 1024,
        }
        system_messages = [
            message.content for message in request.messages if message.role == "system"
        ]
        if system_messages:
            kwargs["system"] = "\n".join(system_messages)
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": definition["name"],
                    "description": definition["description"],
                    "input_schema": definition["parameters"],
                }
                for definition in definition_dicts(request)
            ]
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        return kwargs

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise RuntimeError(
                "anthropic SDK is not installed; install anthropic or pass a client"
            ) from exc
        self._client = AsyncAnthropic()
        return self._client
