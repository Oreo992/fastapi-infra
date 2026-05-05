import pytest

from infra.plugins.ai import ChatMessage, ChatRequest, ToolDefinition
from infra.plugins.ai.adapters.anthropic import AnthropicAIProvider
from infra.plugins.ai.adapters.gemini import GeminiAIProvider
from infra.plugins.ai.adapters.openai import OpenAIProvider


class FakeOpenAIResponses:
    def __init__(self) -> None:
        self.calls = []

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
    assert client.responses.calls[0]["tools"][0]["name"] == "search"
    assert client.responses.calls[1]["stream"] is True
    assert [chunk.content for chunk in chunks] == ["hel", "lo"]


class FakeAnthropicMessages:
    def __init__(self) -> None:
        self.calls = []

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


class FakeGeminiModels:
    def __init__(self) -> None:
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return type("GeminiResponse", (), {"text": "hello"})()

    async def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)
        for text in ["hel", "lo"]:
            yield type("GeminiChunk", (), {"text": text})()


class FakeGeminiAio:
    def __init__(self) -> None:
        self.models = FakeGeminiModels()


class FakeGeminiClient:
    def __init__(self) -> None:
        self.aio = FakeGeminiAio()


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
