import pytest

from infra.plugins.ai import ChatMessage, ChatRequest, ToolDefinition
from infra.plugins.ai.providers.mock import MockAIProvider


@pytest.mark.asyncio
async def test_mock_chat_replies_to_last_user_message():
    provider = MockAIProvider()
    request = ChatRequest(
        model="mock-model",
        messages=[
            ChatMessage(role="system", content="brief"),
            ChatMessage(role="user", content="hello"),
        ],
    )

    response = await provider.chat(request)

    assert response.provider == "mock"
    assert response.model == "mock-model"
    assert response.content == "mock response: hello"
    assert response.tool_calls == []


@pytest.mark.asyncio
async def test_mock_stream_chat_yields_response_chunks():
    provider = MockAIProvider()
    request = ChatRequest(
        model="mock-model",
        messages=[ChatMessage(role="user", content="hello")],
    )

    chunks = [chunk async for chunk in provider.stream_chat(request)]

    assert [chunk.content for chunk in chunks] == ["mock ", "response: ", "hello"]
    assert all(chunk.provider == "mock" for chunk in chunks)
    assert all(chunk.model == "mock-model" for chunk in chunks)


@pytest.mark.asyncio
async def test_mock_chat_returns_first_tool_call_when_tools_are_present():
    provider = MockAIProvider()
    request = ChatRequest(
        model="mock-model",
        messages=[ChatMessage(role="user", content="search")],
        tools=[ToolDefinition(name="search", description="Search", parameters={})],
    )

    response = await provider.chat(request)

    assert response.content == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"query": "mock"}
