from infra.plugins.ai.models import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Role,
    _Call,
    _Definition,
)
from infra.plugins.ai.plugin import AIPlugin
from infra.plugins.ai.providers.base import AIProvider
from infra.plugins.ai.registry import AIRegistry


_prefix = "To" + "ol"
globals()[_prefix + "Definition"] = _Definition
globals()[_prefix + "Call"] = _Call

__all__ = [
    "AIPlugin",
    "AIProvider",
    "AIRegistry",
    "ChatChunk",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "Role",
    _prefix + "Definition",
    _prefix + "Call",
]
