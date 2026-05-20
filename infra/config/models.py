from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PluginSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class InfraNamespace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugins: dict[str, PluginSettings] = Field(default_factory=dict)


class InfraSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    infra: InfraNamespace = Field(default_factory=InfraNamespace)

    def get_plugin(self, name: str) -> PluginSettings:
        return self.infra.plugins.get(name, PluginSettings())
