from collections.abc import AsyncIterator
from typing import Any

from infra.plugins.ai.adapters._shared import (
    close_client,
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


class OpenAIProvider:
    name = "openai"

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
        response = await maybe_await(self._get_client().responses.create(**self._kwargs(request)))
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=text_from(response),
            tool_calls=tool_calls_from(response),
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
                tool_calls=tool_calls_from(item),
            )

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        response = await maybe_await(
            self._get_client().embeddings.create(
                model=request.model,
                input=request.input,
            )
        )
        return EmbeddingResponse(
            provider=self.name,
            model=getattr(response, "model", request.model),
            embeddings=[list(item.embedding) for item in response.data],
            raw=response,
        )

    async def health_check(self):
        return await model_list_health(self.name, self._get_client().models.list)

    async def aclose(self) -> None:
        await close_client(self._client)
        self._client = None

    def _kwargs(self, request: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "input": message_dicts(request.messages),
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.parameters,
                    "strict": False,
                }
                for definition in request.tools
            ]
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_output_tokens"] = request.max_tokens
        return kwargs

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        kwargs = self._client_kwargs()
        if self._client_factory is not None:
            self._client = self._client_factory(**kwargs)
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "openai SDK is not installed; install openai or pass a client"
            ) from exc
        self._client = AsyncOpenAI(**kwargs)
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
