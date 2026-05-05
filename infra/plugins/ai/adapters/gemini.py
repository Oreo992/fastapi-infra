from collections.abc import AsyncIterator
from typing import Any

from infra.plugins.ai.adapters._shared import maybe_await, text_from
from infra.plugins.ai.models import ChatChunk, ChatRequest, ChatResponse


class GeminiAIProvider:
    name = "gemini"

    def __init__(self, client: Any = None) -> None:
        self._client = client

    async def chat(self, request: ChatRequest) -> ChatResponse:
        response = await maybe_await(
            self._get_client().aio.models.generate_content(**self._kwargs(request))
        )
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=text_from(response),
            raw=response,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        stream = self._get_client().aio.models.generate_content_stream(
            **self._kwargs(request)
        )
        async for item in stream:
            yield ChatChunk(
                provider=self.name,
                model=request.model,
                content=text_from(item),
            )

    def _kwargs(self, request: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "contents": [
                {
                    "role": message.role,
                    "parts": [{"text": message.content}],
                }
                for message in request.messages
            ],
        }
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
        if config:
            kwargs["config"] = config
        return kwargs

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai SDK is not installed; install google-genai or pass a client"
            ) from exc
        self._client = genai.Client()
        return self._client
