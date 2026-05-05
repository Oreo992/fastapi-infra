import inspect
from collections.abc import AsyncIterator
from typing import Any

from infra.plugins.ai.models import ChatMessage, ChatRequest


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def iter_any(value: Any) -> AsyncIterator[Any]:
    value = await maybe_await(value)
    if hasattr(value, "__aiter__"):
        async for item in value:
            yield item
        return
    for item in value:
        yield item


def message_dicts(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }
        if message.name is not None:
            item["name"] = message.name
        items.append(item)
    return items


def definition_dicts(request: ChatRequest) -> list[dict[str, Any]]:
    return [
        {
            "name": definition.name,
            "description": definition.description,
            "parameters": definition.parameters,
        }
        for definition in request.tools
    ]


def text_from(value: Any) -> str:
    output_text = getattr(value, "output_text", None)
    if isinstance(output_text, str):
        return output_text

    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text

    delta = getattr(value, "delta", None)
    if isinstance(delta, str):
        return delta

    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            item_text = getattr(item, "text", None)
            if isinstance(item_text, str):
                parts.append(item_text)
        return "".join(parts)

    return ""
