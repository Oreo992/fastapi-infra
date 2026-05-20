from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.release_checks import PluginReleaseIssue, release_error


class SearchConfig(BaseModel):
    endpoint: str = Field(default="memory://search", min_length=1)
    index_name: str = Field(default="default", min_length=1)
    api_key: str | None = None


class SearchService:
    def __init__(self, config: SearchConfig) -> None:
        self.config = config

    async def search(self, query: str) -> dict[str, Any]:
        return {
            "query": query,
            "endpoint": self.config.endpoint,
            "index": self.config.index_name,
            "hits": [],
        }


class SearchPlugin:
    metadata = PluginMetadata(
        name="search",
        version="0.1.0",
        provides=["search"],
    )
    config_model = SearchConfig
    manifest_hints = {
        "env_vars": ["SEARCH_API_KEY"],
        "local_config_example": {
            "endpoint": "memory://search",
            "index_name": "dev",
        },
        "production_config_example": {
            "endpoint": "https://search.example.com",
            "index_name": "production",
            "api_key": "${SEARCH_API_KEY}",
        },
        "service_keys": {"search": "fastapi_infra_search_plugin.SearchService"},
        "scaffold_files": [
            {
                "path": "app/search.py",
                "content": (
                    "from __future__ import annotations\n\n"
                    "from infra import InfraContext\n\n\n"
                    "async def example_search(infra: InfraContext) -> dict[str, object]:\n"
                    "    search = infra.require('search')\n"
                    "    return await search.search('fastapi-infra')\n"
                ),
            }
        ],
        "scaffold_readme_sections": [
            (
                "## Search Plugin\n\n"
                "This project enables the external `search` plugin from "
                "`fastapi-infra-search-plugin-example`. Keep that package installed "
                "in every runtime environment that starts this app.\n"
            )
        ],
        "release_check_notes": [
            "Production search must use a non-memory endpoint and SEARCH_API_KEY.",
        ],
    }

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config
        if not isinstance(config, SearchConfig):
            config = SearchConfig.model_validate(config or {})
        ctx.services["search"] = SearchService(config)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("search", HealthState.HEALTHY)

    def release_check(
        self,
        settings: InfraSettings,
        config: SearchConfig,
    ) -> list[PluginReleaseIssue]:
        if config.endpoint.startswith("memory://"):
            return [
                release_error(
                    "memory_endpoint",
                    "production search requires a non-memory endpoint",
                )
            ]
        if not config.api_key:
            return [
                release_error(
                    "missing_api_key",
                    "production search requires api_key",
                )
            ]
        return []


__all__ = ["SearchConfig", "SearchPlugin", "SearchService"]
