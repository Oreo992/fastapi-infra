from typing import Any

import pytest

from infra.core.health import HealthState
from infra.plugins.ai import ChatMessage, ChatRequest, EmbeddingRequest, ToolDefinition
from infra.plugins.ai.adapters.anthropic import AnthropicAIProvider
from infra.plugins.ai.adapters.gemini import GeminiAIProvider
from infra.plugins.ai.adapters.openai import OpenAIProvider
from infra.plugins.ai.plugin import AIProviderConfig
from infra.plugins.ai.registry import AIRegistry


class FakeOpenAIResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return [
                type("Chunk", (), {"output_text": "hel"})(),
                type("Chunk", (), {"delta": "lo"})(),
            ]
        return type("Response", (), {"output_text": "hello"})()


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = FakeOpenAIResponses()
        self.embeddings = FakeOpenAIEmbeddings()
        self.models = FakeOpenAIModels()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeOpenAIEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = type("EmbeddingItem", (), {"embedding": [0.1, 0.2, 0.3]})()
        return type("EmbeddingResponse", (), {"data": [item], "model": kwargs["model"]})()


class FakeOpenAIModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list(self):
        self.calls.append({})
        return type("ModelsResponse", (), {"data": [type("Model", (), {"id": "gpt-test"})()]})()


@pytest.mark.asyncio
async def test_openai_adapter_uses_responses_api_for_chat_and_stream():
    client = FakeOpenAIClient()
    provider = OpenAIProvider(client=client)
    request = ChatRequest(
        model="gpt-test",
        messages=[ChatMessage(role="user", content="hello")],
        tools=[ToolDefinition(name="search", description="Search")],
    )

    response = await provider.chat(request)
    chunks = [chunk async for chunk in provider.stream_chat(request)]

    assert response.content == "hello"
    assert client.responses.calls[0]["model"] == "gpt-test"
    assert client.responses.calls[0]["input"][0]["content"] == "hello"
    assert client.responses.calls[0]["tools"][0]["type"] == "function"
    assert client.responses.calls[0]["tools"][0]["name"] == "search"
    assert client.responses.calls[0]["tools"][0]["strict"] is False
    assert client.responses.calls[1]["stream"] is True
    assert [chunk.content for chunk in chunks] == ["hel", "lo"]


@pytest.mark.asyncio
async def test_openai_adapter_extracts_tool_calls_from_chat_and_stream():
    class ToolCallResponses(FakeOpenAIResponses):
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            item = type(
                "OutputItem",
                (),
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "search",
                    "arguments": '{"query": "infra"}',
                },
            )()
            response = type("Response", (), {"output_text": "", "output": [item]})()
            if kwargs.get("stream"):
                return [response]
            return response

    client = FakeOpenAIClient()
    client.responses = ToolCallResponses()
    provider = OpenAIProvider(client=client)
    request = ChatRequest(model="gpt-test", messages=[ChatMessage(role="user", content="hello")])

    response = await provider.chat(request)
    chunks = [chunk async for chunk in provider.stream_chat(request)]

    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"query": "infra"}
    assert chunks[0].tool_calls[0].name == "search"


@pytest.mark.asyncio
async def test_openai_adapter_uses_embeddings_api():
    client = FakeOpenAIClient()
    provider = OpenAIProvider(client=client)
    request = EmbeddingRequest(model="text-embedding-test", input=["hello", "world"])

    response = await provider.embed(request)

    assert response.provider == "openai"
    assert response.model == "text-embedding-test"
    assert response.embeddings == [[0.1, 0.2, 0.3]]
    assert response.raw.model == "text-embedding-test"
    assert client.embeddings.calls == [
        {"model": "text-embedding-test", "input": ["hello", "world"]}
    ]


@pytest.mark.asyncio
async def test_openai_adapter_health_check_probes_models_api():
    client = FakeOpenAIClient()
    provider = OpenAIProvider(client=client)

    status = await provider.health_check()

    assert status.status is HealthState.HEALTHY
    assert status.details == {"provider": "openai", "model_count": 1}
    assert client.models.calls == [{}]


@pytest.mark.asyncio
async def test_openai_adapter_health_check_reports_probe_failure():
    class FailingModels:
        async def list(self):
            raise RuntimeError("openai unavailable")

    client = FakeOpenAIClient()
    client.models = FailingModels()
    provider = OpenAIProvider(client=client)

    status = await provider.health_check()

    assert status.status is HealthState.UNHEALTHY
    assert status.message == "openai unavailable"
    assert status.details == {"provider": "openai"}


def test_openai_adapter_passes_config_to_client_factory():
    calls: list[dict[str, Any]] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakeOpenAIClient()

    provider = OpenAIProvider(
        config=AIProviderConfig(
            api_key="sk-test",
            base_url="https://openai.test/v1",
            timeout=3.5,
        ),
        client_factory=factory,
    )

    assert provider._get_client() is provider._get_client()
    assert calls == [
        {
            "api_key": "sk-test",
            "base_url": "https://openai.test/v1",
            "timeout": 3.5,
        }
    ]


@pytest.mark.asyncio
async def test_openai_adapter_closes_created_client():
    client = FakeOpenAIClient()
    provider = OpenAIProvider(client_factory=lambda **kwargs: client)
    provider._get_client()

    await provider.aclose()

    assert client.closed is True
    assert provider._client is None


class FakeAnthropicMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return type(
            "Message",
            (),
            {"content": [type("Block", (), {"text": "hello"})()]},
        )()

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeAnthropicStream()


class FakeAnthropicStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def text_stream(self):
        for text in ["hel", "lo"]:
            yield text


class FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = FakeAnthropicMessages()
        self.models = FakeAnthropicModels()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeAnthropicModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list(self):
        self.calls.append({})
        return type("ModelsResponse", (), {"data": [type("Model", (), {"id": "claude-test"})()]})()


@pytest.mark.asyncio
async def test_anthropic_adapter_uses_messages_api_for_chat_and_stream():
    client = FakeAnthropicClient()
    provider = AnthropicAIProvider(client=client)
    request = ChatRequest(model="claude-test", messages=[ChatMessage(role="user", content="hello")])

    response = await provider.chat(request)
    chunks = [chunk async for chunk in provider.stream_chat(request)]

    assert response.content == "hello"
    assert client.messages.calls[0]["model"] == "claude-test"
    assert client.messages.calls[0]["messages"][0]["content"] == "hello"
    assert client.messages.calls[1]["model"] == "claude-test"
    assert [chunk.content for chunk in chunks] == ["hel", "lo"]


@pytest.mark.asyncio
async def test_anthropic_adapter_extracts_tool_calls_from_chat_and_stream():
    class ToolCallMessages(FakeAnthropicMessages):
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            block = type(
                "Block",
                (),
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "search",
                    "input": {"query": "infra"},
                },
            )()
            return type("Message", (), {"content": [block]})()

        def stream(self, **kwargs):
            self.calls.append(kwargs)
            return FakeAnthropicToolStream()

    class FakeAnthropicToolStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def text_stream(self):
            if False:
                yield ""

        async def __aiter__(self):
            block = type(
                "Block",
                (),
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "search",
                    "input": {"query": "infra"},
                },
            )()
            yield type("Event", (), {"content": [block]})()

    client = FakeAnthropicClient()
    client.messages = ToolCallMessages()
    provider = AnthropicAIProvider(client=client)
    request = ChatRequest(model="claude-test", messages=[ChatMessage(role="user", content="hello")])

    response = await provider.chat(request)
    chunks = [chunk async for chunk in provider.stream_chat(request)]

    assert response.tool_calls[0].id == "toolu_1"
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"query": "infra"}
    assert chunks[0].tool_calls[0].name == "search"


@pytest.mark.asyncio
async def test_anthropic_adapter_streams_text_and_tool_calls_from_one_event_stream():
    class SharedStreamMessages(FakeAnthropicMessages):
        def stream(self, **kwargs):
            self.calls.append(kwargs)
            return SharedTextAndToolStream()

    class SharedTextAndToolStream:
        def __init__(self) -> None:
            block = type(
                "Block",
                (),
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "search",
                    "input": {"query": "infra"},
                },
            )()
            self.events = [
                type("TextEvent", (), {"delta": "hello"})(),
                type("ToolEvent", (), {"content": [block]})(),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def __aiter__(self):
            while self.events:
                yield self.events.pop(0)

        async def text_stream(self):
            async for event in self:
                text = getattr(event, "delta", "")
                if text:
                    yield text

    client = FakeAnthropicClient()
    client.messages = SharedStreamMessages()
    provider = AnthropicAIProvider(client=client)

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            ChatRequest(model="claude-test", messages=[ChatMessage(role="user", content="hello")])
        )
    ]

    assert chunks[0].content == "hello"
    assert chunks[1].tool_calls[0].name == "search"
    assert chunks[1].tool_calls[0].arguments == {"query": "infra"}


@pytest.mark.asyncio
async def test_anthropic_adapter_embeddings_are_not_supported():
    provider = AnthropicAIProvider(client=FakeAnthropicClient())

    with pytest.raises(NotImplementedError, match="does not support embeddings"):
        await provider.embed(EmbeddingRequest(model="claude-test", input="hello"))


@pytest.mark.asyncio
async def test_anthropic_adapter_health_check_probes_models_api():
    client = FakeAnthropicClient()
    provider = AnthropicAIProvider(client=client)

    status = await provider.health_check()

    assert status.status is HealthState.HEALTHY
    assert status.details == {"provider": "anthropic", "model_count": 1}
    assert client.models.calls == [{}]


def test_anthropic_adapter_passes_config_to_client_factory():
    calls: list[dict[str, Any]] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakeAnthropicClient()

    provider = AnthropicAIProvider(
        config=AIProviderConfig(
            api_key="sk-test",
            base_url="https://anthropic.test",
            timeout=4.5,
        ),
        client_factory=factory,
    )

    provider._get_client()
    assert calls == [
        {
            "api_key": "sk-test",
            "base_url": "https://anthropic.test",
            "timeout": 4.5,
        }
    ]


@pytest.mark.asyncio
async def test_anthropic_adapter_closes_created_client():
    client = FakeAnthropicClient()
    provider = AnthropicAIProvider(client_factory=lambda **kwargs: client)
    provider._get_client()

    await provider.aclose()

    assert client.closed is True
    assert provider._client is None


class FakeGeminiModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return type("GeminiResponse", (), {"text": "hello"})()

    async def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)
        for text in ["hel", "lo"]:
            yield type("GeminiChunk", (), {"text": text})()

    async def embed_content(self, **kwargs):
        self.calls.append(kwargs)
        embedding = type("GeminiEmbedding", (), {"values": [0.4, 0.5, 0.6]})()
        return type(
            "GeminiEmbeddingResponse",
            (),
            {"embeddings": [embedding], "model": kwargs["model"]},
        )()

    async def list(self):
        self.calls.append({"list": True})
        return [type("Model", (), {"name": "models/gemini-test"})()]


class FakeGeminiAio:
    def __init__(self) -> None:
        self.models = FakeGeminiModels()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeGeminiClient:
    def __init__(self) -> None:
        self.aio = FakeGeminiAio()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_gemini_adapter_uses_aio_models_for_chat_and_stream():
    client = FakeGeminiClient()
    provider = GeminiAIProvider(client=client)
    request = ChatRequest(model="gemini-test", messages=[ChatMessage(role="user", content="hello")])

    response = await provider.chat(request)
    chunks = [chunk async for chunk in provider.stream_chat(request)]

    assert response.content == "hello"
    assert client.aio.models.calls[0]["model"] == "gemini-test"
    assert client.aio.models.calls[0]["contents"][0]["parts"][0]["text"] == "hello"
    assert client.aio.models.calls[1]["model"] == "gemini-test"
    assert [chunk.content for chunk in chunks] == ["hel", "lo"]


@pytest.mark.asyncio
async def test_gemini_adapter_maps_system_instruction_and_assistant_history():
    client = FakeGeminiClient()
    provider = GeminiAIProvider(client=client)
    request = ChatRequest(
        model="gemini-test",
        messages=[
            ChatMessage(role="system", content="Answer tersely."),
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="hi"),
            ChatMessage(role="user", content="status"),
        ],
    )

    await provider.chat(request)

    call = client.aio.models.calls[0]
    assert call["config"]["system_instruction"] == "Answer tersely."
    assert call["contents"] == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"text": "hi"}]},
        {"role": "user", "parts": [{"text": "status"}]},
    ]


@pytest.mark.asyncio
async def test_gemini_adapter_rejects_tool_messages_until_function_responses_are_supported():
    provider = GeminiAIProvider(client=FakeGeminiClient())

    with pytest.raises(NotImplementedError, match="tool messages"):
        await provider.chat(
            ChatRequest(
                model="gemini-test",
                messages=[ChatMessage(role="tool", content='{"ok": true}')],
            )
        )


@pytest.mark.asyncio
async def test_gemini_adapter_extracts_tool_calls_from_chat_and_stream():
    class ToolCallGeminiModels(FakeGeminiModels):
        async def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            call = type(
                "FunctionCall",
                (),
                {"id": "gcall_1", "name": "search", "args": {"query": "infra"}},
            )()
            return type("GeminiResponse", (), {"text": "", "function_calls": [call]})()

        async def generate_content_stream(self, **kwargs):
            self.calls.append(kwargs)
            call = type(
                "FunctionCall",
                (),
                {"id": "gcall_1", "name": "search", "args": {"query": "infra"}},
            )()
            yield type("GeminiChunk", (), {"text": "", "function_calls": [call]})()

    client = FakeGeminiClient()
    client.aio.models = ToolCallGeminiModels()
    provider = GeminiAIProvider(client=client)
    request = ChatRequest(model="gemini-test", messages=[ChatMessage(role="user", content="hello")])

    response = await provider.chat(request)
    chunks = [chunk async for chunk in provider.stream_chat(request)]

    assert response.tool_calls[0].id == "gcall_1"
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"query": "infra"}
    assert chunks[0].tool_calls[0].name == "search"


@pytest.mark.asyncio
async def test_gemini_adapter_uses_embed_content_api():
    provider = GeminiAIProvider(client=FakeGeminiClient())

    response = await provider.embed(EmbeddingRequest(model="gemini-embedding", input="hello"))

    assert response.provider == "gemini"
    assert response.model == "gemini-embedding"
    assert response.embeddings == [[0.4, 0.5, 0.6]]
    assert provider._get_client().aio.models.calls[-1] == {
        "model": "gemini-embedding",
        "contents": "hello",
    }


@pytest.mark.asyncio
async def test_gemini_adapter_health_check_probes_models_api():
    client = FakeGeminiClient()
    provider = GeminiAIProvider(client=client)

    status = await provider.health_check()

    assert status.status is HealthState.HEALTHY
    assert status.details == {"provider": "gemini", "model_count": 1}
    assert client.aio.models.calls == [{"list": True}]


def test_gemini_adapter_passes_config_to_client_factory():
    calls: list[dict[str, Any]] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakeGeminiClient()

    provider = GeminiAIProvider(
        config=AIProviderConfig(
            api_key="sk-test",
            base_url="https://gemini.test",
            timeout=5.5,
        ),
        client_factory=factory,
    )

    provider._get_client()
    assert calls == [
        {
            "api_key": "sk-test",
            "base_url": "https://gemini.test",
            "timeout": 5.5,
        }
    ]


@pytest.mark.asyncio
async def test_gemini_adapter_closes_created_client():
    client = FakeGeminiClient()
    provider = GeminiAIProvider(client_factory=lambda **kwargs: client)
    provider._get_client()

    await provider.aclose()

    assert client.aio.closed is True
    assert client.closed is True
    assert provider._client is None


@pytest.mark.asyncio
async def test_ai_registry_routes_embeddings_to_selected_provider():
    client = FakeOpenAIClient()
    registry = AIRegistry()
    registry.register(OpenAIProvider(client=client), default=True)

    response = await registry.embed(EmbeddingRequest(model="text-embedding-test", input="hello"))

    assert response.provider == "openai"
    assert client.embeddings.calls[0]["input"] == "hello"


@pytest.mark.asyncio
async def test_ai_registry_closes_registered_providers():
    openai = OpenAIProvider(client=FakeOpenAIClient())
    anthropic = AnthropicAIProvider(client=FakeAnthropicClient())
    registry = AIRegistry()
    registry.register(openai)
    registry.register(anthropic)

    await registry.aclose()

    assert openai._client is None
    assert anthropic._client is None
