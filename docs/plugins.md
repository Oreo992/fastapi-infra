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
