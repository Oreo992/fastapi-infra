from typing import Literal

from pydantic import BaseModel, ConfigDict

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.observability.service import ObservabilityService
from infra.plugins.release_checks import PluginReleaseIssue, release_warning


class ObservabilityPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics_backend: Literal["memory", "prometheus"] = "memory"
    tracing_backend: Literal["none", "opentelemetry"] = "none"


class ObservabilityPlugin:
    metadata = PluginMetadata(
        name="observability",
        version="1.0.0",
        default_enabled=False,
        provides=["observability"],
    )
    config_model = ObservabilityPluginConfig
    manifest_hints = {
        "recommended_extras": ["observability"],
        "service_keys": {"observability": "infra.plugins.OBSERVABILITY_SERVICE"},
        "local_config_example": {
            "metrics_backend": "memory",
            "tracing_backend": "none",
        },
        "production_config_example": {
            "metrics_backend": "prometheus",
            "tracing_backend": "opentelemetry",
        },
        "release_check_notes": [
            "Production memory metrics and disabled tracing are reported as warnings.",
        ],
    }

    def release_check(
        self,
        settings: InfraSettings,
        config: ObservabilityPluginConfig,
    ) -> list[PluginReleaseIssue]:
        issues: list[PluginReleaseIssue] = []
        if config.metrics_backend == "memory":
            issues.append(
                release_warning(
                    "memory_metrics",
                    "production observability should use metrics_backend='prometheus'",
                )
            )
        if config.tracing_backend == "none":
            issues.append(
                release_warning(
                    "tracing_disabled",
                    "production observability should configure tracing_backend='opentelemetry'",
                )
            )
        return issues

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config
            if isinstance(ctx.config, ObservabilityPluginConfig)
            else ObservabilityPluginConfig()
        )
        ctx.services["observability"] = ObservabilityService(
            ctx.health,
            metrics_backend=config.metrics_backend,
            tracing_backend=config.tracing_backend,
        )

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("observability", HealthState.HEALTHY)
