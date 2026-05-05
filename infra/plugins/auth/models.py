from typing import Any

from pydantic import BaseModel, Field


class Principal(BaseModel):
    subject: str
    scopes: set[str] = Field(default_factory=set)
    claims: dict[str, Any] = Field(default_factory=dict)


class ApiKeyRecord(BaseModel):
    subject: str
    scopes: set[str] = Field(default_factory=set)
    claims: dict[str, Any] = Field(default_factory=dict)
