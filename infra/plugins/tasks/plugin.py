import inspect
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.database.plugin import DatabasePluginConfig
from infra.plugins.provider_extensions import (
    external_provider_names_to_load,
    load_entry_point_provider,
)
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginReleaseIssue,
    enabled_plugin_config,
    provider_certification,
    release_error,
)
from infra.plugins.tasks.adapters.celery import CeleryTaskQueue
from infra.plugins.tasks.adapters.kafka import KafkaTaskQueue
from infra.plugins.tasks.adapters.memory import MemoryTaskQueue
from infra.plugins.tasks.adapters.redis_stream import RedisStreamTaskQueue
from infra.plugins.tasks.adapters.sqs import SqsTaskQueue
from infra.plugins.tasks.registry import TaskQueueBackendRegistry

TASK_QUEUE_BACKEND_ENTRY_POINT_GROUP = "fastapi_infra.task_queue_backends"
BUILTIN_TASK_QUEUE_PROVIDERS = frozenset({"memory", "redis", "sqs", "kafka", "celery"})


class RedisTaskQueueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_service: str = Field(default="database", min_length=1)
    stream_name: str = Field(default="infra:tasks", min_length=1)
    consumer_group: str = Field(default="infra", min_length=1)
    consumer_name: str = Field(default="tasks", min_length=1)
    pending_min_idle_ms: int = Field(default=60_000, ge=0)


class SqsTaskQueueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_url: str = Field(min_length=1)
    region_name: str | None = None
    endpoint_url: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    wait_time_seconds: int = Field(default=0, ge=0, le=20)
    visibility_timeout: int | None = Field(default=None, gt=0, le=43_200)
    message_group_id: str | None = None
    dead_letter_queue_url: str | None = None


class KafkaTaskQueueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_servers: str | list[str] = Field(default="localhost:9092")
    topic: str = Field(min_length=1)
    group_id: str = Field(default="infra-tasks", min_length=1)
    client_id: str = Field(default="fastapi-infra-tasks", min_length=1)
    dead_letter_topic: str | None = None
    poll_timeout_seconds: float = Field(default=1.0, gt=0)


class CeleryTaskQueueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_url: str = Field(min_length=1)
    queue_name: str = Field(default="infra.tasks", min_length=1)
    exchange_name: str = Field(default="infra.tasks", min_length=1)
    routing_key: str = Field(default="infra.tasks", min_length=1)
    dead_letter_queue_name: str | None = None
    poll_timeout_seconds: float = Field(default=1.0, gt=0)


class TasksPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: str = "memory"
    service: str = Field(default="tasks", min_length=1)
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class TasksPlugin:
    metadata = PluginMetadata(
        name="tasks",
        version="1.0.0",
        default_enabled=False,
        provides=["tasks"],
        service_name_config="service",
    )
    config_model = TasksPluginConfig
    manifest_hints = {
        "recommended_extras": ["tasks-redis"],
        "service_keys": {"tasks": "infra.plugins.TASKS_SERVICE"},
        "service_references": {
            "providers.redis.database_service": {
                "default_service": "database",
                "required_when": "default_provider == 'redis' and no Redis client is injected",
                "required_when_config": {"default_provider": "redis"},
                "description": "Database service that provides get_redis_client().",
            }
        },
        "env_vars": [
            "REDIS_URL",
            "AWS_REGION",
            "SQS_TASK_QUEUE_URL",
            "KAFKA_BOOTSTRAP_SERVERS",
            "CELERY_BROKER_URL",
        ],
        "local_config_example": {
            "default_provider": "memory",
        },
        "production_config_example": {
            "default_provider": "redis",
            "providers": {
                "redis": {
                    "database_service": "database",
                    "stream_name": "infra:tasks",
                    "consumer_group": "infra",
                    "consumer_name": "tasks",
                    "pending_min_idle_ms": 60_000,
                }
            },
        },
        "production_dependencies": ["database"],
        "release_check_notes": [
            "Production tasks cannot use the memory backend.",
            "The built-in Redis backend requires database.redis_enabled=true and Redis provider certification.",
            "SQS, Kafka, and Celery backends require their optional dependencies and valid broker configuration.",
        ],
    }

    def __init__(
        self,
        redis: Any | None = None,
        *,
        sqs_client: Any | None = None,
        kafka_producer: Any | None = None,
        kafka_consumer: Any | None = None,
        celery_transport: Any | None = None,
    ) -> None:
        self._redis = redis
        self._sqs_client = sqs_client
        self._kafka_producer = kafka_producer
        self._kafka_consumer = kafka_consumer
        self._celery_transport = celery_transport

    def validate_config(self, config: TasksPluginConfig | None) -> None:
        config = config if isinstance(config, TasksPluginConfig) else TasksPluginConfig()
        provider_names = set(config.providers) | {config.default_provider}
        registered_names = self._validate_builtin_provider_configs(config, provider_names)
        external_provider_names_to_load(
            provider_kind="task queue",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=TASK_QUEUE_BACKEND_ENTRY_POINT_GROUP,
        )

    def release_check(
        self,
        settings: InfraSettings,
        config: TasksPluginConfig,
    ) -> list[PluginReleaseIssue]:
        if config.default_provider == "memory":
            return [
                release_error(
                    "memory_provider",
                    "production tasks should use a durable backend, not memory",
                )
            ]
        if config.default_provider != "redis":
            return []

        database = None
        redis_config = RedisTaskQueueConfig.model_validate(config.providers.get("redis", {}))
        if redis_config.database_service == "database":
            database = enabled_plugin_config(settings, "database", DatabasePluginConfig)
        if database is None:
            return [
                release_error(
                    "redis_backing_required",
                    "Redis task provider requires a Redis client or an enabled database plugin",
                )
            ]
        if not database.config.redis_enabled:
            return [
                release_error(
                    "redis_backing_required",
                    "Redis task provider requires database.config.redis_enabled=true",
                )
            ]
        return []

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: TasksPluginConfig,
    ) -> list[PluginProviderCertification]:
        if config.default_provider == "memory":
            return []
        if config.default_provider == "redis":
            return [provider_certification("database", "redis")]
        return [provider_certification("tasks", config.default_provider)]

    def register(self, ctx: PluginContext) -> None:
        config = self._config(ctx)
        registry = TaskQueueBackendRegistry(default_provider=config.default_provider)
        provider_names = set(config.providers) | {config.default_provider}
        registered_names = self._register_builtin_providers(registry, config, provider_names)
        for provider_name in external_provider_names_to_load(
            provider_kind="task queue",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=TASK_QUEUE_BACKEND_ENTRY_POINT_GROUP,
        ):
            registry.register(
                load_entry_point_provider(
                    TASK_QUEUE_BACKEND_ENTRY_POINT_GROUP,
                    provider_name,
                    config.providers.get(provider_name, {}),
                    required_methods=(
                        "enqueue",
                        "dequeue",
                        "complete",
                        "fail",
                        "retry",
                        "dead_letter",
                        "get",
                    ),
                ),
                default=config.default_provider == provider_name,
            )
        ctx.services[config.service] = registry

    async def startup(self, ctx: PluginContext) -> None:
        config = self._config(ctx)
        if config.default_provider != "redis":
            return None
        service = ctx.services.get(config.service)
        if not isinstance(service, TaskQueueBackendRegistry):
            return None
        try:
            service.provider("redis")
            return None
        except LookupError:
            pass

        redis_config = RedisTaskQueueConfig.model_validate(config.providers.get("redis", {}))
        database = ctx.services.get(redis_config.database_service)
        redis_factory = getattr(database, "get_redis_client", None)
        if database is None or redis_factory is None:
            raise RuntimeError(
                "Redis task provider requires an injected redis client or a database "
                "service with get_redis_client()."
            )

        redis = await redis_factory()
        service.register(self._create_redis_queue(config, redis), default=True)
        return None

    def _create_redis_queue(self, config: TasksPluginConfig, redis: Any) -> RedisStreamTaskQueue:
        redis_config = RedisTaskQueueConfig.model_validate(config.providers.get("redis", {}))
        return RedisStreamTaskQueue(
            redis,
            stream_name=redis_config.stream_name,
            consumer_group=redis_config.consumer_group,
            consumer_name=redis_config.consumer_name,
            pending_min_idle_ms=redis_config.pending_min_idle_ms,
        )

    async def shutdown(self, ctx: PluginContext) -> None:
        config = self._config(ctx)
        service = ctx.services.get(config.service)
        if isinstance(service, TaskQueueBackendRegistry):
            for provider_name in service.names():
                close = getattr(service.provider(provider_name), "close", None)
                if close is None:
                    continue
                result = close()
                if inspect.isawaitable(result):
                    await result
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        config = self._config(ctx)
        service = ctx.services.get(config.service)
        if not isinstance(service, TaskQueueBackendRegistry):
            return ctx.health_status("tasks", HealthState.UNHEALTHY, "task queue registry missing")
        try:
            provider = service.provider(config.default_provider)
        except LookupError as exc:
            return ctx.health_status("tasks", HealthState.UNHEALTHY, str(exc))

        health_check = getattr(provider, "health_check", None)
        if health_check is None:
            return ctx.health_status(
                "tasks",
                HealthState.DEGRADED,
                "task queue does not expose health_check",
            )

        try:
            healthy = bool(await health_check())
        except Exception as exc:
            return ctx.health_status("tasks", HealthState.UNHEALTHY, str(exc))
        if healthy:
            return ctx.health_status(
                "tasks",
                HealthState.HEALTHY,
                details={"provider": config.default_provider, "service": config.service},
            )
        return ctx.health_status(
            "tasks",
            HealthState.UNHEALTHY,
            "task queue health check failed",
            {"provider": config.default_provider, "service": config.service},
        )

    def _config(self, ctx: PluginContext) -> TasksPluginConfig:
        if isinstance(ctx.config, TasksPluginConfig):
            return ctx.config
        return TasksPluginConfig()

    def _validate_builtin_provider_configs(
        self,
        config: TasksPluginConfig,
        provider_names: set[str],
    ) -> set[str]:
        registered_names = set(provider_names & BUILTIN_TASK_QUEUE_PROVIDERS)
        if "redis" in provider_names:
            RedisTaskQueueConfig.model_validate(config.providers.get("redis", {}))
        if "sqs" in provider_names:
            SqsTaskQueueConfig.model_validate(config.providers.get("sqs", {}))
        if "kafka" in provider_names:
            KafkaTaskQueueConfig.model_validate(config.providers.get("kafka", {}))
        if "celery" in provider_names:
            CeleryTaskQueueConfig.model_validate(config.providers.get("celery", {}))
        return registered_names

    def _register_builtin_providers(
        self,
        registry: TaskQueueBackendRegistry,
        config: TasksPluginConfig,
        provider_names: set[str],
    ) -> set[str]:
        registered_names = set(provider_names & BUILTIN_TASK_QUEUE_PROVIDERS)
        if "memory" in provider_names:
            registry.register(MemoryTaskQueue(), default=config.default_provider == "memory")
        if "redis" in provider_names and self._redis is not None:
            registry.register(
                self._create_redis_queue(config, self._redis),
                default=config.default_provider == "redis",
            )
        if "sqs" in provider_names:
            registry.register(
                self._create_sqs_queue(config),
                default=config.default_provider == "sqs",
            )
        if "kafka" in provider_names:
            registry.register(
                self._create_kafka_queue(config),
                default=config.default_provider == "kafka",
            )
        if "celery" in provider_names:
            registry.register(
                self._create_celery_queue(config),
                default=config.default_provider == "celery",
            )
        return registered_names

    def _create_sqs_queue(self, config: TasksPluginConfig) -> SqsTaskQueue:
        sqs_config = SqsTaskQueueConfig.model_validate(config.providers.get("sqs", {}))
        return SqsTaskQueue(
            queue_url=sqs_config.queue_url,
            client=self._sqs_client,
            region_name=sqs_config.region_name,
            endpoint_url=sqs_config.endpoint_url,
            aws_access_key_id=sqs_config.aws_access_key_id,
            aws_secret_access_key=sqs_config.aws_secret_access_key,
            aws_session_token=sqs_config.aws_session_token,
            wait_time_seconds=sqs_config.wait_time_seconds,
            visibility_timeout=sqs_config.visibility_timeout,
            message_group_id=sqs_config.message_group_id,
            dead_letter_queue_url=sqs_config.dead_letter_queue_url,
        )

    def _create_kafka_queue(self, config: TasksPluginConfig) -> KafkaTaskQueue:
        kafka_config = KafkaTaskQueueConfig.model_validate(config.providers.get("kafka", {}))
        return KafkaTaskQueue(
            topic=kafka_config.topic,
            bootstrap_servers=kafka_config.bootstrap_servers,
            group_id=kafka_config.group_id,
            producer=self._kafka_producer,
            consumer=self._kafka_consumer,
            client_id=kafka_config.client_id,
            dead_letter_topic=kafka_config.dead_letter_topic,
            poll_timeout_seconds=kafka_config.poll_timeout_seconds,
        )

    def _create_celery_queue(self, config: TasksPluginConfig) -> CeleryTaskQueue:
        celery_config = CeleryTaskQueueConfig.model_validate(config.providers.get("celery", {}))
        return CeleryTaskQueue(
            broker_url=celery_config.broker_url,
            queue_name=celery_config.queue_name,
            exchange_name=celery_config.exchange_name,
            routing_key=celery_config.routing_key,
            dead_letter_queue_name=celery_config.dead_letter_queue_name,
            transport=self._celery_transport,
            poll_timeout_seconds=celery_config.poll_timeout_seconds,
        )
