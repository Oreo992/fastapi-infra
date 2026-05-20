from collections.abc import AsyncIterator
from typing import Any

from infra.plugins.ai.adapters._shared import (
    close_client,
    maybe_await,
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


class GeminiAIProvider:
    name = "gemini"

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
        response = await maybe_await(
            self._get_client().aio.models.generate_content(**self._kwargs(request))
        )
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=text_from(response),
            tool_calls=tool_calls_from(response),
            raw=response,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        stream = self._get_client().aio.models.generate_content_stream(**self._kwargs(request))
        async for item in stream:
            yield ChatChunk(
                provider=self.name,
                model=request.model,
                content=text_from(item),
                tool_calls=tool_calls_from(item),
            )

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        response = await maybe_await(
            self._get_client().aio.models.embed_content(
                model=request.model,
                contents=request.input,
            )
        )
        return EmbeddingResponse(
            provider=self.name,
            model=getattr(response, "model", request.model),
            embeddings=self._embeddings_from_response(response),
            raw=response,
        )

    async def health_check(self):
        return await model_list_health(self.name, self._get_client().aio.models.list)

    async def aclose(self) -> None:
        aio_client = getattr(self._client, "aio", None)
        if aio_client is not None:
            await close_client(aio_client)
        await close_client(self._client)
        self._client = None

    def _kwargs(self, request: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "contents": self._contents(request),
        }
        config = self._config_kwargs(request)
        system_instruction = self._system_instruction(request)
        if system_instruction is not None:
            config["system_instruction"] = system_instruction
        if config:
            kwargs["config"] = config
        return kwargs

    def _contents(self, request: ChatRequest) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                raise NotImplementedError("gemini adapter does not support tool messages yet")
            contents.append(
                {
                    "role": "model" if message.role == "assistant" else "user",
                    "parts": [{"text": message.content}],
                }
            )
        return contents

    def _config_kwargs(self, request: ChatRequest) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if request.temperature is not None:
            config["temperature"] = request.temperature
        if request.max_tokens is not None:
            config["max_output_tokens"] = request.max_tokens
        if request.tools:
            config["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": definition.name,
                            "description": definition.description,
                            "parameters": definition.parameters,
                        }
                        for definition in request.tools
                    ]
                }
            ]
        return config

    def _system_instruction(self, request: ChatRequest) -> str | None:
        instructions = [message.content for message in request.messages if message.role == "system"]
        if not instructions:
            return None
        return "\n\n".join(instructions)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        kwargs = self._client_kwargs()
        if self._client_factory is not None:
            self._client = self._client_factory(**kwargs)
            return self._client
        try:
            from google import genai  # type: ignore[attr-defined]
        except ImportError as exc:
            raise RuntimeError(
                "google-genai SDK is not installed; install google-genai or pass a client"
            ) from exc
        self._client = genai.Client(**self._sdk_client_kwargs(genai, kwargs))
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

    def _sdk_client_kwargs(self, genai: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        client_kwargs: dict[str, Any] = {}
        api_key = kwargs.get("api_key")
        if api_key is not None:
            client_kwargs["api_key"] = api_key

        http_options: dict[str, object] = {}
        base_url = kwargs.get("base_url")
        if base_url is not None:
            http_options["base_url"] = base_url
        timeout = kwargs.get("timeout")
        if timeout is not None:
            http_options["timeout"] = timeout
        if http_options:
            types = getattr(genai, "types", None)
            http_options_type = getattr(types, "HttpOptions", None)
            client_kwargs["http_options"] = (
                http_options_type(**http_options) if http_options_type is not None else http_options
            )
        return client_kwargs

    def _embeddings_from_response(self, response: Any) -> list[list[float]]:
        raw_embeddings = getattr(response, "embeddings", None)
        if raw_embeddings is None:
            raw_embedding = getattr(response, "embedding", None)
            raw_embeddings = [raw_embedding] if raw_embedding is not None else []

        embeddings: list[list[float]] = []
        for item in raw_embeddings:
            values = getattr(item, "values", None)
            if values is None:
                values = getattr(item, "embedding", None)
            if values is None and isinstance(item, dict):
                values = item.get("values") or item.get("embedding")
            if values is None:
                values = item
            embeddings.append([float(value) for value in values])
        return embeddings
