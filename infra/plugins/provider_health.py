import inspect
from typing import Any

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext


async def provider_health_status(
    ctx: PluginContext,
    service_name: str,
    provider: Any,
    *,
    local_provider_names: set[str],
) -> HealthStatus:
    provider_name = str(getattr(provider, "name", provider.__class__.__name__))
    details = {"provider": provider_name}
    if provider_name in local_provider_names:
        return ctx.health_status(service_name, HealthState.HEALTHY, details=details)

    probe = getattr(provider, "health_check", None)
    if not callable(probe):
        return ctx.health_status(
            service_name,
            HealthState.DEGRADED,
            "external provider configured; upstream is not checked by health",
            details,
        )

    try:
        result = probe()
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        return ctx.health_status(service_name, HealthState.UNHEALTHY, str(exc), details)

    if not isinstance(result, HealthStatus):
        return ctx.health_status(
            service_name,
            HealthState.UNHEALTHY,
            "provider health_check returned an invalid result",
            details,
        )

    return ctx.health_status(
        service_name,
        result.status,
        result.message,
        {**details, **result.details},
    )


async def aggregate_provider_health_status(
    ctx: PluginContext,
    service_name: str,
    providers: list[Any],
    *,
    local_provider_names: set[str],
) -> HealthStatus:
    statuses = [
        await provider_health_status(
            ctx,
            service_name,
            provider,
            local_provider_names=local_provider_names,
        )
        for provider in providers
    ]
    worst = _worst_status(statuses)
    messages = [status.message for status in statuses if status.message]
    return ctx.health_status(
        service_name,
        worst,
        "; ".join(messages) if messages else None,
        {
            "providers": {
                str(status.details.get("provider", status.name)): {
                    "status": status.status.value,
                    "message": status.message,
                    "details": status.details,
                }
                for status in statuses
            }
        },
    )


def _worst_status(statuses: list[HealthStatus]) -> HealthState:
    if any(status.status is HealthState.UNHEALTHY for status in statuses):
        return HealthState.UNHEALTHY
    if any(status.status is HealthState.DEGRADED for status in statuses):
        return HealthState.DEGRADED
    return HealthState.HEALTHY
