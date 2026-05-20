from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    content: str
    name: str | None = None


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    tools: list[ToolDefinition] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None


class ChatResponse(BaseModel):
    provider: str
    model: str
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: Any = None


class ChatChunk(BaseModel):
    provider: str
    model: str
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]


class EmbeddingResponse(BaseModel):
    provider: str
    model: str
    embeddings: list[list[float]]
    raw: Any = None
