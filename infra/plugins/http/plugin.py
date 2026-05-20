from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata


class HTTPPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: Literal["mock", "aiohttp"] = "mock"
    base_url: str = ""
    timeout: float = Field(default=30.0, gt=0)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    instrumentation_service: str = Field(default="observability", min_length=1)
    propagate_trace_headers: bool = True
    mock_status_code: int = Field(default=200, ge=100, le=599)
    mock_body: dict[str, Any] = Field(default_factory=lambda: {"ok": True})

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "mock"} or not parsed.netloc:
            raise ValueError("base_url must be empty or an absolute http(s) or mock URL")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_provider_url(self) -> "HTTPPluginConfig":
        if self.default_provider == "aiohttp" and self.base_url.startswith("mock://"):
            raise ValueError("aiohttp provider base_url must be empty or an absolute http(s) URL")
        return self


def _load_http_client():
    from infra.http.client import HttpClient

    return HttpClient


def _load_mock_http_client():
    from infra.http.client import MockHttpClient

    return MockHttpClient


class HTTPPlugin:
    metadata = PluginMetadata(
        name="http",
        version="1.0.0",
        optional_dependencies=[],
        default_enabled=False,
        provides=["http"],
    )
    config_model = HTTPPluginConfig
    manifest_hints = {
        "recommended_extras": ["http"],
        "service_keys": {"http": "infra.plugins.HTTP_SERVICE"},
        "service_references": {
            "instrumentation_service": {
                "default_service": "observability",
                "optional": True,
                "description": "Optional observability service used for outbound HTTP metrics.",
            }
        },
        "local_config_example": {
            "default_provider": "mock",
            "base_url": "mock://http",
            "timeout": 30.0,
        },
        "production_config_example": {
            "default_provider": "aiohttp",
            "base_url": "https://api.example.com",
            "timeout": 30.0,
            "instrumentation_service": "observability",
            "propagate_trace_headers": True,
        },
    }

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config if isinstance(ctx.config, HTTPPluginConfig) else HTTPPluginConfig()
        if config.default_provider == "mock":
            client_type = _load_mock_http_client()
            ctx.services["http"] = client_type(
                base_url=config.base_url or "mock://http",
                status_code=config.mock_status_code,
                body=config.mock_body,
                headers=config.headers or None,
            )
            return

        client_type = _load_http_client()
        ctx.services["http"] = client_type(
            base_url=config.base_url,
            timeout=config.timeout,
            headers=config.headers,
            instrumentation=ctx.services.get(config.instrumentation_service),
            propagate_trace_headers=config.propagate_trace_headers,
        )

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        client = ctx.services.get("http")
        if client is not None:
            await client.close()

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("http", HealthState.HEALTHY)
