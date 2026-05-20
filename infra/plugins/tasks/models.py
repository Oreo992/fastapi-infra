from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

TaskState = Literal["queued", "running", "completed", "failed", "dead_lettered"]


class TaskEnvelope(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str
    payload: dict[str, Any] | None = None
    idempotency_key: str | None = None
    state: TaskState = "queued"
    error: str | None = None
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    available_at: float = 0
