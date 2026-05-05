# Plugins

Plugins implement the `InfraPlugin` protocol:

```python
class InfraPlugin(Protocol):
    metadata: PluginMetadata
    config_model: type[BaseModel] | None

    def register(self, ctx: PluginContext) -> None: ...
    async def startup(self, ctx: PluginContext) -> None: ...
    async def shutdown(self, ctx: PluginContext) -> None: ...
    async def health_check(self, ctx: PluginContext) -> HealthStatus: ...
```

## Metadata

`PluginMetadata` declares:

- `name`: stable plugin id used by settings and health.
- `version`: plugin implementation version.
- `dependencies`: other plugin names that must be active first.
- `optional_dependencies`: importable packages required only when forced on.
- `default_enabled`: default flag behavior.
- `provides`: service names the plugin registers.

## Feature Flags

Plugins are configured under `InfraSettings.infra.plugins`:

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "payment": {"enabled": False},
            "tasks": {"enabled": True},
        }
    }
)
```

Flag behavior:

- `enabled=True`: force plugin startup; invalid config or missing dependencies raise.
- `enabled=False`: skip plugin registration and mark health as disabled.
- `enabled=None`: auto mode; invalid optional setup disables the plugin instead of failing the app.

## Services

Plugins write services to `ctx.services` during `register()`.

```python
def register(self, ctx: PluginContext) -> None:
    ctx.services["my_service"] = MyService()
```

Services are committed to the global registry only after the plugin starts and passes health checks. This keeps failed plugins from leaking half-initialized services.

## Backend Plugins

`database`, `cache`, and `http` are built in but default-disabled. They lazy
import their heavier implementations only when enabled.

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "database": {
                "enabled": True,
                "config": {
                    "config": {"mysql_host": "localhost"},
                    "connect_on_startup": True,
                },
            },
            "cache": {
                "enabled": True,
                "config": {"namespace": "tenant-a"},
            },
            "http": {
                "enabled": True,
                "config": {
                    "base_url": "https://api.example.com",
                    "timeout": 5.0,
                },
            },
        }
    }
)
```

Forced backend plugins fail fast when required optional packages are missing.
The cache plugin owns the Redis manager created by `CacheService` and closes it
during plugin shutdown.

## Tasks

The tasks plugin defaults to the in-memory queue. Use the Redis Streams adapter
when you need cross-process task handoff:

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "database": {"enabled": True},
            "tasks": {
                "enabled": True,
                "config": {
                    "adapter": "redis",
                    "database_service": "database",
                    "stream_name": "myapp:tasks",
                    "consumer_group": "workers",
                    "consumer_name": "worker-1",
                    "pending_min_idle_ms": 60000,
                },
            },
        }
    }
)
```

The Redis adapter creates the consumer group if needed, performs non-blocking
dequeue calls, and attempts stale pending recovery before reading new messages.
`get()` reads the local cache; use `await get_async(task_id)` to load persisted
state into a new queue instance.

## Observability Routes

The observability plugin registers the in-memory observability service, but HTTP
routes are opt-in. Install them explicitly on the FastAPI app:

```python
from infra.plugins.observability import install_observability_routes

install_observability_routes(app, infra, prefix="/ops")
```

With `prefix="/ops"`, the helper adds:

- `GET /ops/healthz`: returns the aggregated health snapshot.
- `GET /ops/readyz`: returns the aggregated health snapshot under `statuses`.
  It returns `503` only when at least one status is `unhealthy`; degraded,
  disabled, and healthy statuses return `200`.
- `GET /ops/metrics`: returns simple `text/plain` metric lines such as
  `requests_total 3`, `request_seconds_count 2`, and
  `request_seconds_sum 0.5`.

Metric output intentionally stays a simple text exposition when
`prometheus_client` is not used. Metric names are normalized to scrape-safe
identifier characters before they are written. The route helper raises
`RuntimeError` if any target route already exists, including when the helper is
installed more than once for the same prefix.
