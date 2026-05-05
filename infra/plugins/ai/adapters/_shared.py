import json
import inspect
from collections.abc import AsyncIterator
from typing import Any

from infra.plugins.ai.models import ChatMessage, ChatRequest, ToolCall


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


def tool_calls_from(value: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    seen: set[tuple[str, str]] = set()
    for item in _walk_tool_call_candidates(value):
        call = _tool_call_from_item(item)
        if call is None:
            continue
        identity = (call.id, call.name)
        if identity in seen:
            continue
        seen.add(identity)
        calls.append(call)
    return calls


def _walk_tool_call_candidates(value: Any) -> list[Any]:
    if value is None or isinstance(value, str | bytes):
        return []

    candidates = [value]
    for field in (
        "tool_calls",
        "output",
        "content",
        "function_calls",
        "parts",
        "candidates",
    ):
        child = _field(value, field)
        candidates.extend(_walk_child_candidates(child))

    function_call = _field(value, "function_call")
    candidates.extend(_walk_child_candidates(function_call))
    content = _field(value, "content")
    if content is not None:
        candidates.extend(_walk_child_candidates(_field(content, "parts")))
    return candidates


def _walk_child_candidates(value: Any) -> list[Any]:
    if value is None or isinstance(value, str | bytes):
        return []
    if isinstance(value, list | tuple):
        items: list[Any] = []
        for item in value:
            items.extend(_walk_tool_call_candidates(item))
        return items
    return _walk_tool_call_candidates(value)


def _tool_call_from_item(item: Any) -> ToolCall | None:
    name = _field(item, "name")
    arguments = _field(item, "arguments")
    if arguments is None:
        arguments = _field(item, "input")
    if arguments is None:
        arguments = _field(item, "args")

    item_type = _field(item, "type")
    is_tool_call = item_type in {"function_call", "tool_use", "tool_call"} or (
        isinstance(name, str) and arguments is not None
    )
    if not is_tool_call or not isinstance(name, str):
        return None

    call_id = _field(item, "call_id") or _field(item, "id") or name
    return ToolCall(
        id=str(call_id),
        name=name,
        arguments=_normalize_arguments(arguments),
    )


def _normalize_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"raw": arguments}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {"value": arguments}


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
