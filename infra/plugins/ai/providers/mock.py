from collections.abc import AsyncIterator

from infra.plugins.ai.models import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    ToolCall,
)


class MockAIProvider:
    name = "mock"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if request.tools:
            first = request.tools[0]
            return ChatResponse(
                provider=self.name,
                model=request.model,
                content="",
                tool_calls=[
                    ToolCall(
                        id="mock-call-1",
                        name=first.name,
                        arguments={"query": "mock"},
                    )
                ],
            )

        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=f"mock response: {self._last_user_content(request)}",
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        for content in ("mock ", "response: ", self._last_user_content(request)):
            yield ChatChunk(provider=self.name, model=request.model, content=content)

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        raise NotImplementedError("mock provider does not support embeddings")

    def _last_user_content(self, request: ChatRequest) -> str:
        for message in reversed(request.messages):
            if message.role == "user":
                return message.content
        return ""
