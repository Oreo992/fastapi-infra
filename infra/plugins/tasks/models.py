from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


TaskState = Literal["queued", "running", "completed", "failed"]


class TaskEnvelope(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str
    payload: dict[str, Any] | None = None
    state: TaskState = "queued"
    error: str | None = None
