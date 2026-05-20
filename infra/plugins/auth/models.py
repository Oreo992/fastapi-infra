from typing import Any

from pydantic import BaseModel, Field


class Principal(BaseModel):
    subject: str
    scopes: set[str] = Field(default_factory=set)
    roles: set[str] = Field(default_factory=set)
    claims: dict[str, Any] = Field(default_factory=dict)


class HashedApiKeyRecord(BaseModel):
    key_id: str | None = None
    subject: str
    scopes: set[str] = Field(default_factory=set)
    roles: set[str] = Field(default_factory=set)
    claims: dict[str, Any] = Field(default_factory=dict)
    key_hash: str = Field(repr=False)


class JwtSigningKeyRecord(BaseModel):
    key_id: str | None = None
    secret: str = Field(repr=False)
