import asyncio
import math
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.core.services import ServiceKey
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.database.plugin import DatabasePluginConfig
from infra.plugins.provider_extensions import (
    external_provider_names_to_load,
    load_entry_point_provider,
)
from infra.plugins.ratelimit.registry import RateLimitBackendRegistry
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginReleaseIssue,
    enabled_plugin_config,
    provider_certification,
    release_error,
)

RATELIMIT_BACKEND_ENTRY_POINT_GROUP = "fastapi_infra.ratelimit_backends"


class RedisRateLimitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_service: str = Field(default="database", min_length=1)
    key_prefix: str = Field(default="infra:ratelimit", min_length=1)


class RateLimitPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: str = "memory"
    service: str = Field(default="ratelimit", min_length=1)
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class MemoryRateLimiter:
    name = "memory"

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


class RedisRateLimiter:
    name = "redis"

    def __init__(
        self,
        redis: Any,
        *,
        key_prefix: str = "infra:ratelimit",
        now: Any | None = None,
    ) -> None:
        self._redis = redis
        self._key_prefix = key_prefix.rstrip(":")
        self._now = now or time.time

    async def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        if limit <= 0:
            return False
        window = max(1, int(window_seconds))
        bucket = int(float(self._now()) // window)
        redis_key = f"{self._key_prefix}:{key}:{window}:{bucket}"
        count = int(await self._redis.incr(redis_key))
        if count == 1:
            await self._redis.expire(redis_key, window)
        return count <= limit

    async def health_check(self) -> bool:
        return bool(await self._redis.ping())


RateLimitKeyFunc = Callable[[Request], str]


def client_ip_key(request: Request) -> str:
    """Build a rate-limit key from the connecting client IP."""

    if request.client is None or not request.client.host:
        return "client:unknown"
    return f"client:{request.client.host}"


def rate_limit(
    *,
    limit: int,
    window_seconds: float,
    key_func: RateLimitKeyFunc = client_ip_key,
    service: str | ServiceKey[Any] = "ratelimit",
) -> Callable[[Request, Response], Any]:
    """Create a FastAPI dependency that enforces the configured rate limiter."""

    if isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if isinstance(window_seconds, bool) or window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    service_name = _service_name(service)
    if not service_name:
        raise ValueError("service must not be empty")

    async def dependency(request: Request, response: Response) -> None:
        limiter = _rate_limiter_from_request(request, service)
        key = key_func(request)
        allowed = await limiter.allow(key, limit=limit, window_seconds=window_seconds)
        headers = _rate_limit_headers(limit, window_seconds)
        response.headers.update(headers)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
                headers=headers,
            )

    return dependency


def _rate_limiter_from_request(request: Request, service: str | ServiceKey[Any]) -> Any:
    service_name = _service_name(service)
    infra = getattr(request.app.state, "infra", None)
    getter = getattr(infra, "get", None)
    if getter is None:
        raise RuntimeError("rate_limit dependency requires app.state.infra")
    limiter = getter(service)
    if not hasattr(limiter, "allow"):
        raise RuntimeError(f"rate limit service is missing allow(): {service_name}")
    return limiter


def _service_name(service: str | ServiceKey[Any]) -> str:
    if isinstance(service, ServiceKey):
        return service.name
    return service.strip()


def _rate_limit_headers(limit: int, window_seconds: float) -> dict[str, str]:
    retry_after = max(1, math.ceil(window_seconds))
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Window": str(retry_after),
        "Retry-After": str(retry_after),
    }


class RateLimitPlugin:
    metadata = PluginMetadata(
        name="ratelimit",
        version="1.0.0",
        default_enabled=False,
        provides=["ratelimit"],
        service_name_config="service",
    )
    config_model = RateLimitPluginConfig
    manifest_hints = {
        "recommended_extras": ["redis"],
        "service_keys": {"ratelimit": "infra.plugins.RATELIMIT_SERVICE"},
        "service_references": {
            "providers.redis.database_service": {
                "default_service": "database",
                "required_when": "default_provider == 'redis' and no Redis client is injected",
                "required_when_config": {"default_provider": "redis"},
                "description": "Database service that provides get_redis_client().",
            }
        },
        "env_vars": ["REDIS_URL"],
        "local_config_example": {
            "default_provider": "memory",
        },
        "production_config_example": {
            "default_provider": "redis",
            "providers": {
                "redis": {
                    "database_service": "database",
                    "key_prefix": "infra:ratelimit",
                }
            },
        },
        "production_dependencies": ["database"],
        "release_check_notes": [
            "Production rate limiting should use the Redis provider with database.redis_enabled=true.",
        ],
    }

    def __init__(self, redis: Any | None = None) -> None:
        self._redis = redis

    def validate_config(self, config: RateLimitPluginConfig | None) -> None:
        config = config if isinstance(config, RateLimitPluginConfig) else RateLimitPluginConfig()
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "memory" in provider_names:
            registered_names.add("memory")
        if "redis" in provider_names:
            RedisRateLimitConfig.model_validate(config.providers.get("redis", {}))
            registered_names.add("redis")
        external_provider_names_to_load(
            provider_kind="rate limit",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=RATELIMIT_BACKEND_ENTRY_POINT_GROUP,
        )

    def release_check(
        self,
        settings: InfraSettings,
        config: RateLimitPluginConfig,
    ) -> list[PluginReleaseIssue]:
        if config.default_provider == "memory":
            return [
                release_error(
                    "memory_provider",
                    "production rate limiting cannot use the in-memory provider",
                )
            ]
        if config.default_provider != "redis":
            return []

        database = None
        redis_config = RedisRateLimitConfig.model_validate(config.providers.get("redis", {}))
        if redis_config.database_service == "database":
            database = enabled_plugin_config(settings, "database", DatabasePluginConfig)
        if database is None:
            return [
                release_error(
                    "redis_backing_required",
                    "Redis rate limiting requires a Redis client or an enabled database plugin",
                )
            ]
        if not database.config.redis_enabled:
            return [
                release_error(
                    "redis_backing_required",
                    "Redis rate limiting requires database.config.redis_enabled=true",
                )
            ]
        return []

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: RateLimitPluginConfig,
    ) -> list[PluginProviderCertification]:
        if config.default_provider == "memory":
            return []
        if config.default_provider == "redis":
            return [provider_certification("database", "redis")]
        return [provider_certification("ratelimit", config.default_provider)]

    def register(self, ctx: PluginContext) -> None:
        config = self._config(ctx)
        registry = RateLimitBackendRegistry(default_provider=config.default_provider)
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "memory" in provider_names:
            registry.register(MemoryRateLimiter(), default=config.default_provider == "memory")
            registered_names.add("memory")
        if "redis" in provider_names:
            registered_names.add("redis")
            if self._redis is not None:
                registry.register(
                    self._create_redis_limiter(config, self._redis),
                    default=config.default_provider == "redis",
                )
        for provider_name in external_provider_names_to_load(
            provider_kind="rate limit",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=RATELIMIT_BACKEND_ENTRY_POINT_GROUP,
        ):
            registry.register(
                load_entry_point_provider(
                    RATELIMIT_BACKEND_ENTRY_POINT_GROUP,
                    provider_name,
                    config.providers.get(provider_name, {}),
                    required_methods=("allow",),
                ),
                default=config.default_provider == provider_name,
            )
        ctx.services[config.service] = registry

    async def startup(self, ctx: PluginContext) -> None:
        config = self._config(ctx)
        if config.default_provider != "redis":
            return None
        service = ctx.services.get(config.service)
        if not isinstance(service, RateLimitBackendRegistry):
            return None
        try:
            service.provider("redis")
            return None
        except LookupError:
            pass

        redis_config = RedisRateLimitConfig.model_validate(config.providers.get("redis", {}))
        database = ctx.services.get(redis_config.database_service)
        redis_factory = getattr(database, "get_redis_client", None)
        if database is None or redis_factory is None:
            raise RuntimeError(
                "Redis rate limit provider requires an injected redis client or a "
                "database service with get_redis_client()."
            )
        redis = await redis_factory()
        service.register(self._create_redis_limiter(config, redis), default=True)
        return None

    def _create_redis_limiter(
        self,
        config: RateLimitPluginConfig,
        redis: Any,
    ) -> RedisRateLimiter:
        redis_config = RedisRateLimitConfig.model_validate(config.providers.get("redis", {}))
        return RedisRateLimiter(
            redis,
            key_prefix=redis_config.key_prefix,
        )

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        config = self._config(ctx)
        service = ctx.services.get(config.service)
        if not isinstance(service, RateLimitBackendRegistry):
            return ctx.health_status(
                "ratelimit",
                HealthState.UNHEALTHY,
                "rate limiter registry missing",
            )
        try:
            provider = service.provider(config.default_provider)
        except LookupError as exc:
            return ctx.health_status("ratelimit", HealthState.UNHEALTHY, str(exc))

        health_check = getattr(provider, "health_check", None)
        if health_check is None:
            return ctx.health_status(
                "ratelimit",
                HealthState.HEALTHY,
                details={"provider": config.default_provider, "service": config.service},
            )
        try:
            healthy = bool(await health_check())
        except Exception as exc:
            return ctx.health_status("ratelimit", HealthState.UNHEALTHY, str(exc))
        if healthy:
            return ctx.health_status(
                "ratelimit",
                HealthState.HEALTHY,
                details={"provider": config.default_provider, "service": config.service},
            )
        return ctx.health_status(
            "ratelimit",
            HealthState.UNHEALTHY,
            "rate limiter health check failed",
            {"provider": config.default_provider, "service": config.service},
        )

    def _config(self, ctx: PluginContext) -> RateLimitPluginConfig:
        if isinstance(ctx.config, RateLimitPluginConfig):
            return ctx.config
        return RateLimitPluginConfig()
