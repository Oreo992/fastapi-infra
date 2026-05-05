import importlib.util

from pydantic import BaseModel

from infra.config.models import InfraSettings
from infra.core.health import HealthRegistry, HealthState, HealthStatus
from infra.plugins.contract import InfraPlugin, PluginContext


class PluginDependencyError(RuntimeError):
    pass


class PluginManager:
    def __init__(self, settings: InfraSettings, plugins: list[InfraPlugin]) -> None:
        self.settings = settings
        self.plugins: dict[str, InfraPlugin] = {}
        for plugin in plugins:
            name = plugin.metadata.name
            if name in self.plugins:
                raise PluginDependencyError(f"duplicate plugin name: {name}")
            self.plugins[name] = plugin
        self.services: dict[str, object] = {}
        self.health = HealthRegistry()
        self.started_plugins: list[str] = []
        self.active_plugins: set[str] = set()
        self._contexts: dict[str, PluginContext] = {}

    def get(self, name: str, default: object = None) -> object:
        return self.services.get(name, default)

    async def startup(self) -> None:
        try:
            for name in self._resolve_order():
                plugin = self.plugins[name]
                plugin_settings = self.settings.get_plugin(name)
                enabled = plugin_settings.enabled

                if enabled is False or (enabled is None and plugin.metadata.default_enabled is False):
                    self._set_disabled(name, "disabled by config")
                    continue

                inactive_dependencies = [
                    dependency
                    for dependency in plugin.metadata.dependencies
                    if dependency not in self.active_plugins
                ]
                if inactive_dependencies:
                    message = (
                        "inactive required dependency: "
                        f"{', '.join(inactive_dependencies)}"
                    )
                    if enabled is True:
                        raise PluginDependencyError(message)
                    self._set_disabled(
                        name,
                        message,
                        details={"inactive_dependencies": inactive_dependencies},
                    )
                    continue

                missing_optional = self._missing_optional_dependencies(plugin)
                if missing_optional:
                    message = f"missing optional dependency: {', '.join(missing_optional)}"
                    if enabled is True:
                        raise PluginDependencyError(message)
                    self._set_disabled(
                        name,
                        message,
                        details={"missing_optional_dependencies": missing_optional},
                    )
                    continue

                config = self._validate_config(plugin, plugin_settings.config)
                ctx = PluginContext(
                    settings=self.settings,
                    plugin_settings=plugin_settings,
                    services=self.services,
                    config=config,
                )
                self._contexts[name] = ctx
                plugin.register(ctx)
                self.services.update(ctx.services)
                await plugin.startup(ctx)
                self.services.update(ctx.services)
                self.started_plugins.append(name)
                self.active_plugins.add(name)
                self.health.set_status(await plugin.health_check(ctx))
                self.services.update(ctx.services)
        except Exception:
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        for name in reversed(self.started_plugins):
            plugin = self.plugins[name]
            ctx = self._contexts[name]
            await plugin.shutdown(ctx)
            self.active_plugins.discard(name)

    def _resolve_order(self) -> list[str]:
        resolved: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise PluginDependencyError(f"circular plugin dependency: {name}")
            plugin = self.plugins.get(name)
            if plugin is None:
                raise PluginDependencyError(f"unknown plugin dependency: {name}")

            visiting.add(name)
            for dependency in plugin.metadata.dependencies:
                if dependency not in self.plugins:
                    raise PluginDependencyError(f"unknown plugin dependency: {dependency}")
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            resolved.append(name)

        for name in self.plugins:
            visit(name)

        return resolved

    def _missing_optional_dependencies(self, plugin: InfraPlugin) -> list[str]:
        missing: list[str] = []
        for dependency in plugin.metadata.optional_dependencies:
            module_name = dependency.replace("-", "_")
            if importlib.util.find_spec(module_name) is None:
                missing.append(dependency)
        return missing

    def _validate_config(
        self,
        plugin: InfraPlugin,
        config: dict[str, object],
    ) -> BaseModel | None:
        if plugin.config_model is None:
            return None
        return plugin.config_model.model_validate(config)

    def _set_disabled(
        self,
        name: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        self.health.set_status(
            HealthStatus(
                name=name,
                status=HealthState.DISABLED,
                message=message,
                details=details or {},
            )
        )
