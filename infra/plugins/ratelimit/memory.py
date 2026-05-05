import asyncio
import time

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata


class MemoryRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        if limit <= 0:
            return False

        now = time.monotonic()
        window_start = now - max(window_seconds, 0)
        async with self._lock:
            hits = [hit for hit in self._hits.get(key, []) if hit > window_start]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


class RateLimitPlugin:
    metadata = PluginMetadata(
        name="ratelimit",
        version="1.0.0",
        provides=["ratelimit"],
    )
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["ratelimit"] = MemoryRateLimiter()

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("ratelimit", HealthState.HEALTHY)
