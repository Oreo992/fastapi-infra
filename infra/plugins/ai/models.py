from typing import Any, Literal

from pydantic import BaseModel, Field


Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    content: str
    name: str | None = None


class _Definition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class _Call(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    tools: list[_Definition] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None


class ChatResponse(BaseModel):
    provider: str
    model: str
    content: str
    tool_calls: list[_Call] = Field(default_factory=list)
    raw: Any = None


class ChatChunk(BaseModel):
    provider: str
    model: str
    content: str = ""
    tool_calls: list[_Call] = Field(default_factory=list)
