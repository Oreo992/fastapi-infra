import asyncio
import importlib.util
from dataclasses import dataclass
from typing import TypeGuard

from pydantic import BaseModel, ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthRegistry, HealthState, HealthStatus
from infra.plugins.contract import (
    InfraPlugin,
    PluginConfigValidatorHook,
    PluginContext,
    resolve_plugin_manifest_hints,
)


@dataclass(frozen=True)
class _StartupSnapshot:
    services: dict[str, object]
    contexts: dict[str, PluginContext]
    started_plugins: list[str]
    active_plugins: set[str]
    started: bool


class _DisabledPlugin:
    pass


_DISABLED_PLUGIN = _DisabledPlugin()


class PluginDependencyError(RuntimeError):
    pass


def _has_config_validator(plugin: object) -> TypeGuard[PluginConfigValidatorHook]:
    return callable(getattr(plugin, "validate_config", None))


class PluginManager:
    def __init__(
        self,
        settings: InfraSettings,
        plugins: list[InfraPlugin],
        *,
        health_check_timeout_seconds: float | None = 5.0,
    ) -> None:
        if health_check_timeout_seconds is not None and health_check_timeout_seconds < 0:
            raise ValueError("health_check_timeout_seconds must be greater than or equal to 0")
        self.settings = settings
        self.health_check_timeout_seconds = health_check_timeout_seconds
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
        self._started = False

    def get(self, name: str, default: object = None) -> object:
        return self.services.get(name, default)

    def manifest(self) -> dict[str, dict[str, object]]:
        items: dict[str, dict[str, object]] = {}
        for name, plugin in self.plugins.items():
            metadata = plugin.metadata
            plugin_settings = self.settings.get_plugin(name)
            config_model = plugin.config_model
            items[name] = {
                "name": metadata.name,
                "version": metadata.version,
                "default_enabled": metadata.default_enabled,
                "dependencies": list(metadata.dependencies),
                "optional_dependencies": list(metadata.optional_dependencies),
                "provides": list(metadata.provides),
                "service_name_config": metadata.service_name_config,
                "configured_services": self._manifest_configured_services(
                    metadata.provides,
                    metadata.service_name_config,
                    plugin_settings.config,
                ),
                "configured_enabled": plugin_settings.enabled,
                "config_model": config_model.__name__ if config_model is not None else None,
                "config_schema": (
                    config_model.model_json_schema() if config_model is not None else None
                ),
                **self._manifest_hints(plugin),
            }
        return items

    def _manifest_hints(self, plugin: InfraPlugin) -> dict[str, object]:
        return resolve_plugin_manifest_hints(plugin).model_dump()

    def _manifest_configured_services(
        self,
        provides: list[str],
        service_name_config: str | None,
        config: dict[str, object],
    ) -> list[str]:
        services = list(provides)
        if service_name_config is not None:
            configured_service = config.get(service_name_config)
            if isinstance(configured_service, str):
                normalized = configured_service.strip()
                if normalized and normalized not in services:
                    services.append(normalized)
        return services

    async def startup(self) -> None:
        if self._started:
            raise RuntimeError("plugin manager is already started")

        self._check_unknown_configured_plugins()
        snapshot = self._startup_snapshot()
        try:
            for name in self._resolve_order():
                await self._startup_plugin(name)
            self._started = True
        except Exception:
            await self._rollback_startup(snapshot)
            raise

    async def refresh_health(
        self,
        *,
        timeout_seconds: float | None = 5.0,
    ) -> dict[str, HealthStatus]:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be greater than or equal to 0")

        health_statuses = await asyncio.gather(
            *(
                self._plugin_health_status(name, timeout_seconds=timeout_seconds)
                for name in list(self.started_plugins)
                if name in self.active_plugins and name in self._contexts
            )
        )
        for health_status in health_statuses:
            self.health.set_status(health_status)
        return self.health.snapshot()

    async def _plugin_health_status(
        self,
        name: str,
        *,
        timeout_seconds: float | None,
    ) -> HealthStatus:
        plugin = self.plugins[name]
        ctx = self._contexts[name]
        try:
            health_check = plugin.health_check(ctx)
            if timeout_seconds is None:
                return await health_check
            return await asyncio.wait_for(health_check, timeout=timeout_seconds)
        except TimeoutError:
            return ctx.health_status(
                name,
                HealthState.UNHEALTHY,
                f"health check timed out after {timeout_seconds}s",
            )
        except Exception as exc:
            return ctx.health_status(name, HealthState.UNHEALTHY, str(exc))

    def _check_unknown_configured_plugins(self) -> None:
        unknown_configured_plugins = sorted(set(self.settings.infra.plugins) - set(self.plugins))
        if unknown_configured_plugins:
            raise PluginDependencyError(
                "unknown configured plugin: " + ", ".join(unknown_configured_plugins)
            )

    def _startup_snapshot(self) -> _StartupSnapshot:
        return _StartupSnapshot(
            services=dict(self.services),
            contexts=dict(self._contexts),
            started_plugins=list(self.started_plugins),
            active_plugins=set(self.active_plugins),
            started=self._started,
        )

    async def _rollback_startup(self, snapshot: _StartupSnapshot) -> None:
        try:
            await self.shutdown()
        except Exception:
            pass
        self.services.clear()
        self.services.update(snapshot.services)
        self._contexts = snapshot.contexts
        self.started_plugins = snapshot.started_plugins
        self.active_plugins = snapshot.active_plugins
        self._started = snapshot.started

    async def _startup_plugin(self, name: str) -> None:
        plugin = self.plugins[name]
        plugin_settings = self.settings.get_plugin(name)
        enabled = plugin_settings.enabled

        if enabled is False or (enabled is None and not plugin.metadata.default_enabled):
            self._set_disabled(name, "disabled by config")
            return

        if self._disable_or_raise_for_dependency_errors(name, plugin, enabled):
            return

        config = self._validated_plugin_config_or_disabled(name, plugin, enabled)
        if isinstance(config, _DisabledPlugin):
            return

        ctx = PluginContext(
            settings=self.settings,
            plugin_settings=plugin_settings,
            services=dict(self.services),
            health=self.health,
            config=config,
        )
        before_register_services = dict(ctx.services)
        self._contexts[name] = ctx
        plugin.register(ctx)
        self._validate_registered_services(name, before_register_services, ctx)
        self.started_plugins.append(name)
        await plugin.startup(ctx)
        self._validate_registered_services(name, before_register_services, ctx)
        self.active_plugins.add(name)
        health_status = await self._plugin_health_status(
            name,
            timeout_seconds=self.health_check_timeout_seconds,
        )
        self.health.set_status(health_status)
        if health_status.status is HealthState.UNHEALTHY:
            message = f"plugin is unhealthy: {name}"
            if health_status.message:
                message = f"{message} ({health_status.message})"
            raise PluginDependencyError(message)
        self.services.clear()
        self.services.update(ctx.services)

    def _disable_or_raise_for_dependency_errors(
        self,
        name: str,
        plugin: InfraPlugin,
        enabled: bool | None,
    ) -> bool:
        missing_dependencies = [
            dependency
            for dependency in plugin.metadata.dependencies
            if dependency not in self.plugins
        ]
        if missing_dependencies:
            message = "unknown required dependency: " f"{', '.join(missing_dependencies)}"
            if enabled is True:
                raise PluginDependencyError(message)
            self._set_disabled(
                name,
                message,
                details={"missing_dependencies": missing_dependencies},
            )
            return True

        inactive_dependencies = [
            dependency
            for dependency in plugin.metadata.dependencies
            if dependency not in self.active_plugins
        ]
        if inactive_dependencies:
            message = "inactive required dependency: " f"{', '.join(inactive_dependencies)}"
            if enabled is True:
                raise PluginDependencyError(message)
            self._set_disabled(
                name,
                message,
                details={"inactive_dependencies": inactive_dependencies},
            )
            return True

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
            return True

        return False

    def _validated_plugin_config_or_disabled(
        self,
        name: str,
        plugin: InfraPlugin,
        enabled: bool | None,
    ) -> BaseModel | None | _DisabledPlugin:
        plugin_settings = self.settings.get_plugin(name)
        try:
            return self._validate_config(plugin, plugin_settings.config)
        except (ValidationError, ValueError) as exc:
            if enabled is not None:
                raise
            self._set_disabled(
                name,
                "invalid plugin config",
                details={"config_error": str(exc)},
            )
            return _DISABLED_PLUGIN

    async def shutdown(self) -> None:
        first_error: Exception | None = None
        for name in reversed(list(self.started_plugins)):
            plugin = self.plugins[name]
            ctx = self._contexts.get(name)
            if ctx is None:
                self.started_plugins.remove(name)
                self.active_plugins.discard(name)
                continue
            try:
                await plugin.shutdown(ctx)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                continue

            self.started_plugins.remove(name)
            self._contexts.pop(name, None)
            self.active_plugins.discard(name)

        if first_error is not None:
            raise first_error
        self.services.clear()
        self._started = False

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
                if dependency in self.plugins:
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
        validated_config = plugin.config_model.model_validate(config)
        if _has_config_validator(plugin):
            plugin.validate_config(validated_config)
        return validated_config

    def _validate_registered_services(
        self,
        name: str,
        before_register_services: dict[str, object],
        ctx: PluginContext,
    ) -> None:
        before_keys = set(before_register_services)
        after_keys = set(ctx.services)
        removed = before_keys - after_keys
        if removed:
            raise PluginDependencyError(
                f"plugin {name} removed services it does not own: {', '.join(sorted(removed))}"
            )

        overwritten = [
            service_name
            for service_name in before_keys & after_keys
            if ctx.services[service_name] is not before_register_services[service_name]
        ]
        if overwritten:
            raise PluginDependencyError(
                f"plugin {name} overwrote existing services: {', '.join(sorted(overwritten))}"
            )

        added = after_keys - before_keys
        allowed = set(self.plugins[name].metadata.provides)
        configured_service_name = self._configured_service_name(name, ctx)
        if configured_service_name is not None:
            allowed.add(configured_service_name)
        undeclared = added - allowed
        if undeclared:
            raise PluginDependencyError(
                f"plugin {name} registered undeclared services: " f"{', '.join(sorted(undeclared))}"
            )

    def _configured_service_name(self, name: str, ctx: PluginContext) -> str | None:
        field_name = self.plugins[name].metadata.service_name_config
        if field_name is None or ctx.config is None:
            return None
        value = getattr(ctx.config, field_name, None)
        return value if isinstance(value, str) else None

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
