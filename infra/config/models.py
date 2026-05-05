from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PluginSettings(BaseModel):
    enabled: bool | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class InfraNamespace(BaseModel):
    plugins: dict[str, PluginSettings] = Field(default_factory=dict)


class InfraSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    infra: InfraNamespace = Field(default_factory=InfraNamespace)

    def get_plugin(self, name: str) -> PluginSettings:
        return self.infra.plugins.get(name, PluginSettings())
