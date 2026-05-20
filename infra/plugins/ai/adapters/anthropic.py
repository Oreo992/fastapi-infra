from collections.abc import AsyncIterator
from typing import Any

from infra.plugins.ai.adapters._shared import (
    close_client,
    definition_dicts,
    iter_any,
    maybe_await,
    message_dicts,
    model_list_health,
    text_from,
    tool_calls_from,
)
from infra.plugins.ai.models import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
)


class AnthropicAIProvider:
    name = "anthropic"

    def __init__(
        self,
        client: Any = None,
        *,
        config: Any = None,
        client_factory: Any = None,
    ) -> None:
        self._client = client
        self._config = config
        self._client_factory = client_factory

    async def chat(self, request: ChatRequest) -> ChatResponse:
        response = await maybe_await(self._get_client().messages.create(**self._kwargs(request)))
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=text_from(response),
            tool_calls=tool_calls_from(response),
            raw=response,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        async with self._get_client().messages.stream(**self._kwargs(request)) as stream:
            if hasattr(stream, "__aiter__"):
                async for item in iter_any(stream):
                    content = text_from(item)
                    tool_calls = tool_calls_from(item)
                    if content or tool_calls:
                        yield ChatChunk(
                            provider=self.name,
                            model=request.model,
                            content=content,
                            tool_calls=tool_calls,
                        )
                return
            text_stream = stream.text_stream
            chunks = text_stream() if callable(text_stream) else text_stream
            async for text in chunks:
                yield ChatChunk(provider=self.name, model=request.model, content=text)

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        raise NotImplementedError("anthropic provider does not support embeddings")

    async def health_check(self):
        return await model_list_health(self.name, self._get_client().models.list)

    async def aclose(self) -> None:
        await close_client(self._client)
        self._client = None

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
        kwargs = self._client_kwargs()
        if self._client_factory is not None:
            self._client = self._client_factory(**kwargs)
            return self._client
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise RuntimeError(
                "anthropic SDK is not installed; install anthropic or pass a client"
            ) from exc
        self._client = AsyncAnthropic(**kwargs)
        return self._client

    def _client_kwargs(self) -> dict[str, Any]:
        if self._config is None:
            return {}
        if hasattr(self._config, "client_kwargs"):
            return dict(self._config.client_kwargs())
        return {
            key: value
            for key in ("api_key", "base_url", "timeout")
            if (value := getattr(self._config, key, None)) is not None
        }
