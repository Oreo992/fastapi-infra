# FastAPI Infra Plugin Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild `fastapi-infra` into a clean plugin-based FastAPI infrastructure platform with a single setup API, dynamic feature flags, health aggregation, and first-batch plugins.

**Architecture:** The core package exposes `InfraSettings` and `setup_infra(app, settings)`. Optional capabilities live behind `InfraPlugin` contracts and are loaded by `PluginManager` through tri-state flags, dependency ordering, lifecycle hooks, and health checks. No v0.1 compatibility facades are kept.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, pytest, official OpenAI/Anthropic/Google GenAI SDK adapters behind lazy imports, Redis Streams adapter behind optional plugin dependency.

---

## File Map

Core files:

- Create `infra/core/__init__.py`: public core exports.
- Create `infra/core/context.py`: `InfraContext` runtime object and service accessor.
- Create `infra/core/flags.py`: tri-state `FeatureFlag` resolution.
- Create `infra/core/health.py`: structured `HealthStatus` and `HealthRegistry`.
- Create `infra/core/app.py`: `setup_infra(app, settings)` orchestration.
- Create `infra/config/models.py`: `InfraSettings`, `PluginSettings`, and plugin config lookup.
- Modify `infra/config/__init__.py`: export `InfraSettings`.
- Modify `infra/__init__.py`: export only the new public API.

Plugin framework:

- Create `infra/plugins/contract.py`: `InfraPlugin`, `PluginMetadata`, `PluginContext`.
- Create `infra/plugins/manager.py`: plugin registration, flag resolution, dependency sorting, lifecycle, and health aggregation.
- Create `infra/plugins/builtin.py`: built-in plugin manifest.
- Modify `infra/plugins/__init__.py`: export plugin framework symbols.

First-batch plugins:

- Create `infra/plugins/ai/`: AI DTOs, provider protocol, registry, mock provider, SDK adapters, plugin wrapper.
- Create `infra/plugins/auth/`: principal models, API key/JWT minimal auth, plugin wrapper.
- Create `infra/plugins/observability/`: health route helpers and plugin wrapper.
- Create `infra/plugins/tasks/`: queue protocol, memory adapter, Redis Streams adapter, plugin wrapper.
- Create `infra/plugins/storage/`: storage protocol, local adapter, plugin wrapper.
- Create `infra/plugins/webhooks/`: signature verification, event dispatcher, plugin wrapper.
- Create `infra/plugins/payment/`: payment protocol, mock provider, plugin wrapper.
- Create `infra/plugins/ratelimit/`: memory limiter, middleware/dependency helpers, plugin wrapper.
- Create `infra/plugins/notifications/`: notification protocol, no-op provider, plugin wrapper.

Cleanup and public surface:

- Remove business-coupled `infra/startup/*.py` modules except a generic lifecycle replacement.
- Remove business-specific exception and error-code exports.
- Modify `pyproject.toml`: split optional dependencies by plugin.
- Rewrite `README.md`.
- Create `docs/architecture.md`, `docs/plugins.md`, `docs/ai.md`.
- Replace examples with `examples/minimal/`, `examples/ai_app/`, and `examples/full_stack/`.

Tests:

- Create `tests/core/`
- Create `tests/plugins/`
- Create `tests/examples/`
- Create `tests/test_no_business_imports.py`

## Parallel Ownership

The work is suitable for subagent-driven development:

- Worker 1 owns tasks 1-3: core settings, flags, plugin manager, setup, health.
- Worker 2 owns task 4: breaking cleanup and public API cleanup.
- Worker 3 owns task 5: AI plugin and tests.
- Worker 4 owns tasks 6-7: auth, observability, tasks.
- Worker 5 owns task 8: storage, webhooks, payment, ratelimit, notifications.
- Worker 6 owns tasks 9-10: docs, examples, final verification.

Workers must not revert changes made by other workers. Each worker should edit only its assigned files and adjust to the new public API created by earlier tasks.

### Task 1: Settings, Feature Flags, And Health Models

**Files:**
- Create: `infra/core/__init__.py`
- Create: `infra/core/flags.py`
- Create: `infra/core/health.py`
- Create: `infra/config/models.py`
- Modify: `infra/config/__init__.py`
- Test: `tests/core/test_settings_flags_health.py`

- [ ] **Step 1: Write the failing settings and flag tests**

Create `tests/core/test_settings_flags_health.py`:

```python
from infra.config.models import InfraSettings, PluginSettings
from infra.core.flags import FeatureFlag, resolve_feature_flag
from infra.core.health import HealthRegistry, HealthState, HealthStatus


def test_plugin_settings_default_to_auto():
    settings = PluginSettings()
    assert settings.enabled is None
    assert settings.config == {}


def test_infra_settings_reads_plugin_namespace():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {"default_provider": "mock"},
                }
            }
        }
    )
    plugin = settings.get_plugin("ai")
    assert plugin.enabled is True
    assert plugin.config == {"default_provider": "mock"}


def test_missing_plugin_uses_auto_settings():
    settings = InfraSettings()
    plugin = settings.get_plugin("payment")
    assert plugin.enabled is None
    assert plugin.config == {}


def test_feature_flag_resolution():
    assert resolve_feature_flag(True) is FeatureFlag.ENABLED
    assert resolve_feature_flag(False) is FeatureFlag.DISABLED
    assert resolve_feature_flag(None) is FeatureFlag.AUTO


def test_health_registry_tracks_disabled_and_healthy_statuses():
    registry = HealthRegistry()
    registry.set_status(HealthStatus(name="ai", status=HealthState.HEALTHY))
    registry.set_status(
        HealthStatus(name="payment", status=HealthState.DISABLED, message="disabled by config")
    )

    result = registry.snapshot()

    assert result["ai"].status is HealthState.HEALTHY
    assert result["payment"].status is HealthState.DISABLED
    assert result["payment"].message == "disabled by config"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/core/test_settings_flags_health.py -v`

Expected: FAIL with `ModuleNotFoundError` for `infra.config.models` or `infra.core`.

- [ ] **Step 3: Implement settings, flags, and health models**

Create `infra/core/flags.py`:

```python
from enum import Enum


class FeatureFlag(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    AUTO = "auto"


def resolve_feature_flag(value: bool | None) -> FeatureFlag:
    if value is True:
        return FeatureFlag.ENABLED
    if value is False:
        return FeatureFlag.DISABLED
    return FeatureFlag.AUTO
```

Create `infra/core/health.py`:

```python
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


class HealthStatus(BaseModel):
    name: str
    status: HealthState
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class HealthRegistry:
    def __init__(self) -> None:
        self._statuses: dict[str, HealthStatus] = {}

    def set_status(self, status: HealthStatus) -> None:
        self._statuses[status.name] = status

    def snapshot(self) -> dict[str, HealthStatus]:
        return dict(self._statuses)
```

Create `infra/config/models.py`:

```python
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PluginSettings(BaseModel):
    enabled: bool | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class InfraNamespace(BaseModel):
    plugins: dict[str, PluginSettings] = Field(default_factory=dict)


class InfraSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    infra: InfraNamespace = Field(default_factory=InfraNamespace)

    def get_plugin(self, name: str) -> PluginSettings:
        return self.infra.plugins.get(name, PluginSettings())
```

Create `infra/core/__init__.py`:

```python
from infra.core.flags import FeatureFlag, resolve_feature_flag
from infra.core.health import HealthRegistry, HealthState, HealthStatus

__all__ = [
    "FeatureFlag",
    "HealthRegistry",
    "HealthState",
    "HealthStatus",
    "resolve_feature_flag",
]
```

Modify `infra/config/__init__.py` to export both the existing base class and new settings:

```python
from infra.config.settings import BaseSettings
from infra.config.models import InfraSettings, PluginSettings

__all__ = ["BaseSettings", "InfraSettings", "PluginSettings"]
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `pytest tests/core/test_settings_flags_health.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/core infra/config/models.py infra/config/__init__.py tests/core/test_settings_flags_health.py
git commit -m "feat: add infra settings flags and health models"
```

### Task 2: Plugin Contract And Manager

**Files:**
- Create: `infra/plugins/contract.py`
- Create: `infra/plugins/manager.py`
- Create: `infra/plugins/builtin.py`
- Modify: `infra/plugins/__init__.py`
- Test: `tests/core/test_plugin_manager.py`

- [ ] **Step 1: Write failing plugin manager tests**

Create `tests/core/test_plugin_manager.py`:

```python
import pytest
from pydantic import BaseModel

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.manager import PluginManager


class FakeConfig(BaseModel):
    value: str = "ok"


class FakePlugin:
    metadata = PluginMetadata(name="fake", version="1.0.0", provides=["fake"])
    config_model = FakeConfig

    def __init__(self) -> None:
        self.events: list[str] = []

    def register(self, ctx: PluginContext) -> None:
        self.events.append("register")
        ctx.services["fake"] = self

    async def startup(self, ctx: PluginContext) -> None:
        self.events.append("startup")

    async def shutdown(self, ctx: PluginContext) -> None:
        self.events.append("shutdown")

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("fake", HealthState.HEALTHY)


class DependentPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="dependent",
        version="1.0.0",
        dependencies=["fake"],
        provides=["dependent"],
    )


class MissingDependencyPlugin(FakePlugin):
    metadata = PluginMetadata(
        name="missing",
        version="1.0.0",
        optional_dependencies=["package_that_does_not_exist_fastapi_infra"],
        default_enabled=None,
        provides=["missing"],
    )


@pytest.mark.asyncio
async def test_enabled_plugin_registers_starts_and_stops():
    settings = InfraSettings(infra={"plugins": {"fake": {"enabled": True}}})
    plugin = FakePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()
    await manager.shutdown()

    assert plugin.events == ["register", "startup", "shutdown"]
    assert manager.get("fake") is plugin


@pytest.mark.asyncio
async def test_disabled_plugin_is_not_registered_or_started():
    settings = InfraSettings(infra={"plugins": {"fake": {"enabled": False}}})
    plugin = FakePlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    assert plugin.events == []
    assert manager.get("fake", default=None) is None
    assert manager.health.snapshot()["fake"].status is HealthState.DISABLED


@pytest.mark.asyncio
async def test_dependency_order_for_startup_and_reverse_shutdown():
    settings = InfraSettings(
        infra={
            "plugins": {
                "fake": {"enabled": True},
                "dependent": {"enabled": True},
            }
        }
    )
    fake = FakePlugin()
    dependent = DependentPlugin()
    manager = PluginManager(settings=settings, plugins=[dependent, fake])

    await manager.startup()
    await manager.shutdown()

    assert fake.events == ["register", "startup", "shutdown"]
    assert dependent.events == ["register", "startup", "shutdown"]
    assert list(manager.started_plugins) == ["fake", "dependent"]


@pytest.mark.asyncio
async def test_auto_plugin_skips_when_optional_dependency_is_missing():
    settings = InfraSettings(infra={"plugins": {"missing": {"enabled": None}}})
    plugin = MissingDependencyPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    await manager.startup()

    assert plugin.events == []
    assert manager.health.snapshot()["missing"].status is HealthState.DISABLED


@pytest.mark.asyncio
async def test_forced_plugin_fails_when_optional_dependency_is_missing():
    settings = InfraSettings(infra={"plugins": {"missing": {"enabled": True}}})
    plugin = MissingDependencyPlugin()
    manager = PluginManager(settings=settings, plugins=[plugin])

    with pytest.raises(Exception, match="missing optional dependency"):
        await manager.startup()
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/core/test_plugin_manager.py -v`

Expected: FAIL with `ModuleNotFoundError` for `infra.plugins.contract` or `infra.plugins.manager`.

- [ ] **Step 3: Implement plugin contract**

Create `infra/plugins/contract.py`:

```python
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field

from infra.config.models import InfraSettings, PluginSettings
from infra.core.health import HealthState, HealthStatus


class PluginMetadata(BaseModel):
    name: str
    version: str
    dependencies: list[str] = Field(default_factory=list)
    optional_dependencies: list[str] = Field(default_factory=list)
    default_enabled: bool | None = None
    provides: list[str] = Field(default_factory=list)


class PluginContext(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    settings: InfraSettings
    plugin_settings: PluginSettings
    services: dict[str, Any]

    def health_status(
        self,
        name: str,
        status: HealthState,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> HealthStatus:
        return HealthStatus(
            name=name,
            status=status,
            message=message,
            details=details or {},
        )


class InfraPlugin(Protocol):
    metadata: PluginMetadata
    config_model: type[BaseModel] | None

    def register(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def startup(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def shutdown(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        raise NotImplementedError
```

- [ ] **Step 4: Implement plugin manager**

Create `infra/plugins/manager.py`:

```python
from collections.abc import Iterable
from importlib.util import find_spec
from typing import Any

from infra.config.models import InfraSettings
from infra.core.flags import FeatureFlag, resolve_feature_flag
from infra.core.health import HealthRegistry, HealthState, HealthStatus
from infra.plugins.contract import InfraPlugin, PluginContext


class PluginDependencyError(RuntimeError):
    pass


class PluginManager:
    def __init__(self, settings: InfraSettings, plugins: Iterable[InfraPlugin]) -> None:
        self.settings = settings
        self.plugins = {plugin.metadata.name: plugin for plugin in plugins}
        self.services: dict[str, Any] = {}
        self.health = HealthRegistry()
        self.started_plugins: list[str] = []
        self._contexts: dict[str, PluginContext] = {}

    def get(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    async def startup(self) -> None:
        for name in self._resolve_order():
            plugin = self.plugins[name]
            plugin_settings = self.settings.get_plugin(name)
            flag = resolve_feature_flag(
                plugin_settings.enabled
                if plugin_settings.enabled is not None
                else plugin.metadata.default_enabled
            )
            if flag is FeatureFlag.DISABLED:
                self.health.set_status(
                    HealthStatus(
                        name=name,
                        status=HealthState.DISABLED,
                        message="disabled by config",
                    )
                )
                continue
            missing_dependencies = self._missing_optional_dependencies(plugin)
            if missing_dependencies and flag is FeatureFlag.AUTO:
                self.health.set_status(
                    HealthStatus(
                        name=name,
                        status=HealthState.DISABLED,
                        message="missing optional dependencies",
                        details={"missing": missing_dependencies},
                    )
                )
                continue
            if missing_dependencies and flag is FeatureFlag.ENABLED:
                raise PluginDependencyError(
                    f"missing optional dependency for {name}: {', '.join(missing_dependencies)}"
                )

            ctx = PluginContext(
                settings=self.settings,
                plugin_settings=plugin_settings,
                services=self.services,
            )
            self._contexts[name] = ctx
            plugin.register(ctx)
            await plugin.startup(ctx)
            self.started_plugins.append(name)
            self.health.set_status(await plugin.health_check(ctx))

    async def shutdown(self) -> None:
        for name in reversed(self.started_plugins):
            plugin = self.plugins[name]
            await plugin.shutdown(self._contexts[name])

    def _resolve_order(self) -> list[str]:
        resolved: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise PluginDependencyError(f"circular plugin dependency: {name}")
            if name not in self.plugins:
                raise PluginDependencyError(f"unknown plugin dependency: {name}")
            visiting.add(name)
            for dep in self.plugins[name].metadata.dependencies:
                visit(dep)
            visiting.remove(name)
            visited.add(name)
            resolved.append(name)

        for plugin_name in self.plugins:
            visit(plugin_name)
        return resolved

    def _missing_optional_dependencies(self, plugin: InfraPlugin) -> list[str]:
        missing: list[str] = []
        for package in plugin.metadata.optional_dependencies:
            module_name = package.replace("-", "_")
            if find_spec(module_name) is None:
                missing.append(package)
        return missing
```

Create `infra/plugins/builtin.py`:

```python
from infra.plugins.contract import InfraPlugin


def get_builtin_plugins() -> list[InfraPlugin]:
    return []
```

Modify `infra/plugins/__init__.py`:

```python
from infra.plugins.contract import InfraPlugin, PluginContext, PluginMetadata
from infra.plugins.manager import PluginDependencyError, PluginManager

__all__ = [
    "InfraPlugin",
    "PluginContext",
    "PluginDependencyError",
    "PluginManager",
    "PluginMetadata",
]
```

- [ ] **Step 5: Run tests and verify they pass**

Run: `pytest tests/core/test_plugin_manager.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add infra/plugins tests/core/test_plugin_manager.py
git commit -m "feat: add plugin contract and manager"
```

### Task 3: Setup API And Infra Context

**Files:**
- Create: `infra/core/context.py`
- Create: `infra/core/app.py`
- Modify: `infra/core/__init__.py`
- Modify: `infra/__init__.py`
- Test: `tests/core/test_setup_infra.py`

- [ ] **Step 1: Write failing setup tests**

Create `tests/core/test_setup_infra.py`:

```python
from fastapi import FastAPI

from infra import InfraSettings, setup_infra
from infra.core.context import InfraContext
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata


class SimplePlugin:
    metadata = PluginMetadata(name="simple", version="1.0.0", default_enabled=True)
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["simple"] = {"ready": True}

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("simple", HealthState.HEALTHY)


def test_setup_infra_attaches_context_to_app():
    app = FastAPI()
    settings = InfraSettings()

    infra = setup_infra(app, settings, plugins=[SimplePlugin()])

    assert isinstance(infra, InfraContext)
    assert app.state.infra is infra
    assert infra.get("simple") is None


def test_context_get_returns_registered_service_after_manual_startup():
    app = FastAPI()
    settings = InfraSettings()
    infra = setup_infra(app, settings, plugins=[SimplePlugin()])

    import anyio

    anyio.run(infra.startup)

    assert infra.get("simple") == {"ready": True}
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/core/test_setup_infra.py -v`

Expected: FAIL with import errors for `setup_infra` or `InfraContext`.

- [ ] **Step 3: Implement context and setup API**

Create `infra/core/context.py`:

```python
from typing import Any

from fastapi import FastAPI

from infra.config.models import InfraSettings
from infra.core.health import HealthRegistry
from infra.plugins.contract import InfraPlugin
from infra.plugins.manager import PluginManager


class InfraContext:
    def __init__(
        self,
        app: FastAPI,
        settings: InfraSettings,
        plugins: list[InfraPlugin],
    ) -> None:
        self.app = app
        self.settings = settings
        self.plugin_manager = PluginManager(settings=settings, plugins=plugins)
        self.health: HealthRegistry = self.plugin_manager.health

    async def startup(self) -> None:
        await self.plugin_manager.startup()

    async def shutdown(self) -> None:
        await self.plugin_manager.shutdown()

    def get(self, name: str, default: Any = None) -> Any:
        return self.plugin_manager.get(name, default)
```

Create `infra/core/app.py`:

```python
from fastapi import FastAPI

from infra.config.models import InfraSettings
from infra.core.context import InfraContext
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.contract import InfraPlugin


def setup_infra(
    app: FastAPI,
    settings: InfraSettings | None = None,
    plugins: list[InfraPlugin] | None = None,
) -> InfraContext:
    resolved_settings = settings or InfraSettings()
    resolved_plugins = plugins if plugins is not None else get_builtin_plugins()
    context = InfraContext(app=app, settings=resolved_settings, plugins=resolved_plugins)
    app.state.infra = context

    @app.on_event("startup")
    async def _infra_startup() -> None:
        await context.startup()

    @app.on_event("shutdown")
    async def _infra_shutdown() -> None:
        await context.shutdown()

    return context
```

Modify `infra/core/__init__.py`:

```python
from infra.core.app import setup_infra
from infra.core.context import InfraContext
from infra.core.flags import FeatureFlag, resolve_feature_flag
from infra.core.health import HealthRegistry, HealthState, HealthStatus

__all__ = [
    "FeatureFlag",
    "HealthRegistry",
    "HealthState",
    "HealthStatus",
    "InfraContext",
    "resolve_feature_flag",
    "setup_infra",
]
```

Modify `infra/__init__.py`:

```python
from infra.config import InfraSettings, PluginSettings
from infra.core import InfraContext, setup_infra

__version__ = "0.2.0"

__all__ = [
    "InfraContext",
    "InfraSettings",
    "PluginSettings",
    "setup_infra",
]
```

- [ ] **Step 4: Run setup tests**

Run: `pytest tests/core/test_setup_infra.py -v`

Expected: PASS, with possible FastAPI `on_event` deprecation warnings accepted for this batch.

- [ ] **Step 5: Commit**

```bash
git add infra/core infra/__init__.py tests/core/test_setup_infra.py
git commit -m "feat: add setup_infra public API"
```

### Task 4: Breaking Cleanup And Business Import Removal

**Files:**
- Delete or replace: `infra/startup/*.py` files with business imports.
- Modify: `infra/exceptions/base.py`
- Modify: `infra/exceptions/__init__.py`
- Modify: `infra/common/contracts.py`
- Modify: `infra/database/manager.py`
- Modify: `infra/cache/service.py`
- Modify: `infra/streaming/streams_manager.py`
- Test: `tests/test_no_business_imports.py`
- Test: `tests/core/test_public_api_clean.py`

- [ ] **Step 1: Write failing business import scan**

Create `tests/test_no_business_imports.py`:

```python
from pathlib import Path


def test_infra_package_has_no_original_business_imports():
    root = Path("infra")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from app." in text or "import app." in text:
            offenders.append(str(path))
        if "PersonalityTest" in text or "MusicSync" in text:
            offenders.append(str(path))

    assert offenders == []
```

Create `tests/core/test_public_api_clean.py`:

```python
import infra


def test_top_level_public_api_is_small_and_explicit():
    assert sorted(infra.__all__) == [
        "InfraContext",
        "InfraSettings",
        "PluginSettings",
        "setup_infra",
    ]
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_no_business_imports.py tests/core/test_public_api_clean.py -v`

Expected: FAIL listing `infra/startup` and business exceptions.

- [ ] **Step 3: Remove business-coupled startup modules**

Delete these files if they contain `app.*` imports:

```text
infra/startup/alerts.py
infra/startup/database.py
infra/startup/monitoring.py
infra/startup/queues.py
infra/startup/register_callbacks.py
infra/startup/services.py
infra/startup/shutdown.py
infra/startup/tasks.py
infra/startup/tools.py
infra/startup/warmup.py
```

Keep a generic lifecycle module only if it has no business import. If keeping the package, make `infra/startup/__init__.py` contain:

```python
from infra.startup.lifecycle import LifecycleManager, create_lifecycle_manager

__all__ = ["LifecycleManager", "create_lifecycle_manager"]
```

- [ ] **Step 4: Replace exception exports with infrastructure-only errors**

Modify `infra/exceptions/base.py` so it contains only:

```python
from typing import Any


class AppException(Exception):
    def __init__(
        self,
        message: str,
        error_code: str = "INTERNAL_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(message)


class ConfigurationError(AppException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, "CONFIGURATION_ERROR", details)


class PluginError(AppException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, "PLUGIN_ERROR", details)


class AuthenticationError(AppException):
    def __init__(self, message: str = "authentication failed") -> None:
        super().__init__(message, "UNAUTHORIZED")


class AuthorizationError(AppException):
    def __init__(self, message: str = "permission denied") -> None:
        super().__init__(message, "FORBIDDEN")


class ExternalServiceError(AppException):
    def __init__(self, service_name: str, message: str) -> None:
        super().__init__(
            message=f"{service_name}: {message}",
            error_code="EXTERNAL_SERVICE_ERROR",
            details={"service_name": service_name},
        )
```

Modify `infra/exceptions/__init__.py`:

```python
from infra.exceptions.base import (
    AppException,
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ExternalServiceError,
    PluginError,
)

__all__ = [
    "AppException",
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "ExternalServiceError",
    "PluginError",
]
```

- [ ] **Step 5: Replace common error codes with infrastructure-only codes**

Modify `infra/common/contracts.py` so `ErrorCode` includes only:

```python
class ErrorCode(str, Enum):
    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    PLUGIN_ERROR = "PLUGIN_ERROR"
    EXTERNAL_SERVICE_ERROR = "EXTERNAL_SERVICE_ERROR"
```

Keep existing generic response models if they do not import business terms.

- [ ] **Step 6: Remove coupled resource managers from the public API**

Modify `infra/database/__init__.py` so it no longer exports a global manager or compatibility functions:

```python
__all__: list[str] = []
```

Modify `infra/cache/__init__.py` so it no longer exports a Redis-backed cache service from the core package:

```python
__all__: list[str] = []
```

Modify `infra/streaming/__init__.py` so Redis Streams are not part of the core public API:

```python
__all__: list[str] = []
```

The Redis Streams implementation can be reused by `infra.plugins.tasks` through a plugin-owned adapter import. It must not be imported from `infra.__init__`.

- [ ] **Step 7: Run cleanup tests**

Run: `pytest tests/test_no_business_imports.py tests/core/test_public_api_clean.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add infra tests/test_no_business_imports.py tests/core/test_public_api_clean.py
git commit -m "refactor: remove legacy business coupling"
```

### Task 5: AI Plugin With Mock And SDK Adapters

**Files:**
- Create: `infra/plugins/ai/__init__.py`
- Create: `infra/plugins/ai/models.py`
- Create: `infra/plugins/ai/providers.py`
- Create: `infra/plugins/ai/registry.py`
- Create: `infra/plugins/ai/plugin.py`
- Create: `infra/plugins/ai/adapters/mock.py`
- Create: `infra/plugins/ai/adapters/openai.py`
- Create: `infra/plugins/ai/adapters/anthropic.py`
- Create: `infra/plugins/ai/adapters/gemini.py`
- Modify: `infra/plugins/builtin.py`
- Test: `tests/plugins/test_ai_mock_provider.py`
- Test: `tests/plugins/test_ai_plugin.py`

- [ ] **Step 1: Write failing AI mock provider tests**

Create `tests/plugins/test_ai_mock_provider.py`:

```python
import pytest

from infra.plugins.ai.adapters.mock import MockAIProvider
from infra.plugins.ai.models import ChatMessage, ChatRequest, ToolDefinition


@pytest.mark.asyncio
async def test_mock_chat_returns_deterministic_response():
    provider = MockAIProvider()
    request = ChatRequest(
        model="mock-model",
        messages=[ChatMessage(role="user", content="hello")],
    )

    response = await provider.chat(request)

    assert response.provider == "mock"
    assert response.content == "mock response: hello"
    assert response.tool_calls == []


@pytest.mark.asyncio
async def test_mock_stream_chat_yields_chunks():
    provider = MockAIProvider()
    request = ChatRequest(
        model="mock-model",
        messages=[ChatMessage(role="user", content="hello")],
    )

    chunks = [chunk async for chunk in provider.stream_chat(request)]

    assert [chunk.content for chunk in chunks] == ["mock ", "response: ", "hello"]


@pytest.mark.asyncio
async def test_mock_tool_call_uses_declared_tool_name():
    provider = MockAIProvider()
    request = ChatRequest(
        model="mock-model",
        messages=[ChatMessage(role="user", content="use a tool")],
        tools=[
            ToolDefinition(
                name="lookup",
                description="lookup data",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            )
        ],
    )

    response = await provider.chat(request)

    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"query": "mock"}
```

- [ ] **Step 2: Write failing AI plugin registration test**

Create `tests/plugins/test_ai_plugin.py`:

```python
import pytest

from infra.config.models import InfraSettings
from infra.plugins.ai.plugin import AIPlugin
from infra.plugins.manager import PluginManager


@pytest.mark.asyncio
async def test_ai_plugin_registers_registry_and_default_mock_provider():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {"default_provider": "mock", "providers": {"mock": {}}},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AIPlugin()])

    await manager.startup()
    registry = manager.get("ai")
    response = await registry.chat_text("hello")

    assert response.content == "mock response: hello"
```

- [ ] **Step 3: Run tests and verify they fail**

Run: `pytest tests/plugins/test_ai_mock_provider.py tests/plugins/test_ai_plugin.py -v`

Expected: FAIL with missing `infra.plugins.ai`.

- [ ] **Step 4: Implement AI DTOs and provider protocol**

Create `infra/plugins/ai/models.py`:

```python
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    content: str
    name: str | None = None


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    tools: list[ToolDefinition] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None


class ChatResponse(BaseModel):
    provider: str
    model: str
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: Any = None


class ChatChunk(BaseModel):
    provider: str
    model: str
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class AIProvider(Protocol):
    name: str

    async def chat(self, request: ChatRequest) -> ChatResponse:
        raise NotImplementedError

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError
```

- [ ] **Step 5: Implement mock provider and registry**

Create `infra/plugins/ai/adapters/mock.py`:

```python
from collections.abc import AsyncIterator

from infra.plugins.ai.models import ChatChunk, ChatRequest, ChatResponse, ToolCall


class MockAIProvider:
    name = "mock"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        last_user = next(
            (message.content for message in reversed(request.messages) if message.role == "user"),
            "",
        )
        tool_calls = []
        if request.tools:
            tool_calls.append(
                ToolCall(
                    id="mock-tool-call-1",
                    name=request.tools[0].name,
                    arguments={"query": "mock"},
                )
            )
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=f"mock response: {last_user}",
            tool_calls=tool_calls,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        last_user = next(
            (message.content for message in reversed(request.messages) if message.role == "user"),
            "",
        )
        for content in ["mock ", "response: ", last_user]:
            yield ChatChunk(provider=self.name, model=request.model, content=content)
```

Create `infra/plugins/ai/registry.py`:

```python
from infra.plugins.ai.models import AIProvider, ChatMessage, ChatRequest, ChatResponse


class AIProviderRegistry:
    def __init__(self, default_provider: str) -> None:
        self.default_provider = default_provider
        self._providers: dict[str, AIProvider] = {}

    def register(self, provider: AIProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str | None = None) -> AIProvider:
        provider_name = name or self.default_provider
        if provider_name not in self._providers:
            raise KeyError(f"AI provider not registered: {provider_name}")
        return self._providers[provider_name]

    async def chat(self, request: ChatRequest, provider: str | None = None) -> ChatResponse:
        return await self.get(provider).chat(request)

    async def chat_text(self, text: str, provider: str | None = None) -> ChatResponse:
        request = ChatRequest(
            model="mock-model",
            messages=[ChatMessage(role="user", content=text)],
        )
        return await self.chat(request, provider=provider)
```

- [ ] **Step 6: Implement AI plugin**

Create `infra/plugins/ai/plugin.py`:

```python
from pydantic import BaseModel, Field

from infra.core.health import HealthState
from infra.plugins.ai.adapters.mock import MockAIProvider
from infra.plugins.ai.registry import AIProviderRegistry
from infra.plugins.contract import PluginContext, PluginMetadata


class AIPluginConfig(BaseModel):
    default_provider: str = "mock"
    providers: dict[str, dict] = Field(default_factory=lambda: {"mock": {}})


class AIPlugin:
    metadata = PluginMetadata(
        name="ai",
        version="1.0.0",
        default_enabled=False,
        provides=["ai"],
    )
    config_model = AIPluginConfig

    def __init__(self) -> None:
        self.registry: AIProviderRegistry | None = None

    def register(self, ctx: PluginContext) -> None:
        config = AIPluginConfig(**ctx.plugin_settings.config)
        registry = AIProviderRegistry(default_provider=config.default_provider)
        if "mock" in config.providers:
            registry.register(MockAIProvider())
        self.registry = registry
        ctx.services["ai"] = registry

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        providers = sorted(self.registry._providers) if self.registry else []
        return ctx.health_status(
            "ai",
            HealthState.HEALTHY,
            details={"providers": providers},
        )
```

Create `infra/plugins/ai/__init__.py`:

```python
from infra.plugins.ai.models import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ToolCall,
    ToolDefinition,
)
from infra.plugins.ai.plugin import AIPlugin
from infra.plugins.ai.registry import AIProviderRegistry

__all__ = [
    "AIPlugin",
    "AIProviderRegistry",
    "ChatChunk",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ToolCall",
    "ToolDefinition",
]
```

- [ ] **Step 7: Add SDK adapters with lazy imports and injectable clients**

Create `infra/plugins/ai/adapters/openai.py`:

```python
from collections.abc import AsyncIterator

from infra.exceptions import ConfigurationError
from infra.plugins.ai.models import ChatChunk, ChatMessage, ChatRequest, ChatResponse, ToolCall


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None, model: str, client=None) -> None:
        if client is None and not api_key:
            raise ConfigurationError("openai api_key is required")
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)
        self.client = client
        self.model = model

    async def chat(self, request: ChatRequest) -> ChatResponse:
        response = await self.client.responses.create(
            model=request.model or self.model,
            input=[message.model_dump(exclude_none=True) for message in request.messages],
            tools=[self._tool_to_openai(tool) for tool in request.tools] or None,
        )
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=getattr(response, "output_text", ""),
            tool_calls=[],
            raw=response,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        stream = await self.client.responses.create(
            model=request.model or self.model,
            input=[message.model_dump(exclude_none=True) for message in request.messages],
            tools=[self._tool_to_openai(tool) for tool in request.tools] or None,
            stream=True,
        )
        async for event in stream:
            if getattr(event, "type", "") == "response.output_text.delta":
                yield ChatChunk(
                    provider=self.name,
                    model=request.model,
                    content=getattr(event, "delta", ""),
                )

    def _tool_to_openai(self, tool):
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
```

Create `infra/plugins/ai/adapters/anthropic.py`:

```python
from collections.abc import AsyncIterator

from infra.exceptions import ConfigurationError
from infra.plugins.ai.models import ChatChunk, ChatRequest, ChatResponse


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None, model: str, client=None) -> None:
        if client is None and not api_key:
            raise ConfigurationError("anthropic api_key is required")
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key)
        self.client = client
        self.model = model

    async def chat(self, request: ChatRequest) -> ChatResponse:
        response = await self.client.messages.create(
            model=request.model or self.model,
            max_tokens=request.max_tokens or 1024,
            messages=[message.model_dump(exclude_none=True) for message in request.messages if message.role != "system"],
            tools=[self._tool_to_anthropic(tool) for tool in request.tools] or None,
        )
        text_parts = [
            getattr(part, "text", "")
            for part in getattr(response, "content", [])
            if getattr(part, "type", "") == "text"
        ]
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content="".join(text_parts),
            raw=response,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        async with self.client.messages.stream(
            model=request.model or self.model,
            max_tokens=request.max_tokens or 1024,
            messages=[message.model_dump(exclude_none=True) for message in request.messages if message.role != "system"],
            tools=[self._tool_to_anthropic(tool) for tool in request.tools] or None,
        ) as stream:
            async for text in stream.text_stream:
                yield ChatChunk(provider=self.name, model=request.model, content=text)

    def _tool_to_anthropic(self, tool):
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }
```

Create `infra/plugins/ai/adapters/gemini.py`:

```python
from collections.abc import AsyncIterator

from infra.exceptions import ConfigurationError
from infra.plugins.ai.models import ChatChunk, ChatRequest, ChatResponse


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None, model: str, client=None) -> None:
        if client is None and not api_key:
            raise ConfigurationError("gemini api_key is required")
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self.client = client
        self.model = model

    async def chat(self, request: ChatRequest) -> ChatResponse:
        response = await self.client.aio.models.generate_content(
            model=request.model or self.model,
            contents=[message.content for message in request.messages],
        )
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=getattr(response, "text", ""),
            raw=response,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        stream = await self.client.aio.models.generate_content_stream(
            model=request.model or self.model,
            contents=[message.content for message in request.messages],
        )
        async for chunk in stream:
            yield ChatChunk(
                provider=self.name,
                model=request.model,
                content=getattr(chunk, "text", ""),
            )
```

Create `tests/plugins/test_ai_sdk_adapters.py` with fake clients that assert each adapter calls the expected SDK surface. The tests should inject fake clients through the `client` constructor argument, so the official SDK packages are not required for unit tests.

- [ ] **Step 8: Register AI in built-in manifest**

Modify `infra/plugins/builtin.py`:

```python
from infra.plugins.ai.plugin import AIPlugin
from infra.plugins.contract import InfraPlugin


def get_builtin_plugins() -> list[InfraPlugin]:
    return [AIPlugin()]
```

- [ ] **Step 9: Run AI tests**

Run: `pytest tests/plugins/test_ai_mock_provider.py tests/plugins/test_ai_plugin.py -v`

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add infra/plugins/ai infra/plugins/builtin.py tests/plugins/test_ai_mock_provider.py tests/plugins/test_ai_plugin.py
git commit -m "feat: add ai plugin with mock provider"
```

### Task 6: Auth Plugin Minimal Implementation

**Files:**
- Create: `infra/plugins/auth/__init__.py`
- Create: `infra/plugins/auth/models.py`
- Create: `infra/plugins/auth/service.py`
- Create: `infra/plugins/auth/plugin.py`
- Modify: `infra/plugins/builtin.py`
- Test: `tests/plugins/test_auth_plugin.py`

- [ ] **Step 1: Write failing auth tests**

Create `tests/plugins/test_auth_plugin.py`:

```python
import pytest

from infra.config.models import InfraSettings
from infra.exceptions import AuthenticationError, AuthorizationError
from infra.plugins.auth.plugin import AuthPlugin
from infra.plugins.manager import PluginManager


@pytest.mark.asyncio
async def test_auth_plugin_registers_api_key_authenticator():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {"api_keys": {"secret": {"subject": "svc", "scopes": ["admin"]}}},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AuthPlugin()])

    await manager.startup()
    auth = manager.get("auth")
    principal = auth.authenticate_api_key("secret")

    assert principal.subject == "svc"
    assert principal.scopes == {"admin"}


@pytest.mark.asyncio
async def test_auth_rejects_bad_api_key_and_missing_scope():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {"api_keys": {"secret": {"subject": "svc", "scopes": []}}},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AuthPlugin()])
    await manager.startup()
    auth = manager.get("auth")

    with pytest.raises(AuthenticationError):
        auth.authenticate_api_key("bad")

    principal = auth.authenticate_api_key("secret")
    with pytest.raises(AuthorizationError):
        auth.require_scopes(principal, {"admin"})
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/plugins/test_auth_plugin.py -v`

Expected: FAIL with missing `infra.plugins.auth`.

- [ ] **Step 3: Implement auth models and service**

Create `infra/plugins/auth/models.py`:

```python
from pydantic import BaseModel, Field


class Principal(BaseModel):
    subject: str
    scopes: set[str] = Field(default_factory=set)
    claims: dict = Field(default_factory=dict)


class ApiKeyRecord(BaseModel):
    subject: str
    scopes: set[str] = Field(default_factory=set)
```

Create `infra/plugins/auth/service.py`:

```python
from infra.exceptions import AuthenticationError, AuthorizationError
from infra.plugins.auth.models import ApiKeyRecord, Principal


class AuthService:
    def __init__(self, api_keys: dict[str, ApiKeyRecord]) -> None:
        self.api_keys = api_keys

    def authenticate_api_key(self, api_key: str) -> Principal:
        record = self.api_keys.get(api_key)
        if record is None:
            raise AuthenticationError("invalid api key")
        return Principal(subject=record.subject, scopes=set(record.scopes))

    def require_scopes(self, principal: Principal, required: set[str]) -> None:
        missing = required - principal.scopes
        if missing:
            raise AuthorizationError(f"missing scopes: {', '.join(sorted(missing))}")
```

- [ ] **Step 4: Implement auth plugin**

Create `infra/plugins/auth/plugin.py`:

```python
from pydantic import BaseModel, Field

from infra.core.health import HealthState
from infra.plugins.auth.models import ApiKeyRecord
from infra.plugins.auth.service import AuthService
from infra.plugins.contract import PluginContext, PluginMetadata


class AuthPluginConfig(BaseModel):
    api_keys: dict[str, ApiKeyRecord] = Field(default_factory=dict)


class AuthPlugin:
    metadata = PluginMetadata(
        name="auth",
        version="1.0.0",
        default_enabled=False,
        provides=["auth"],
    )
    config_model = AuthPluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = AuthPluginConfig(**ctx.plugin_settings.config)
        ctx.services["auth"] = AuthService(api_keys=config.api_keys)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        service = ctx.services["auth"]
        return ctx.health_status(
            "auth",
            HealthState.HEALTHY,
            details={"api_key_count": len(service.api_keys)},
        )
```

Create `infra/plugins/auth/__init__.py`:

```python
from infra.plugins.auth.models import Principal
from infra.plugins.auth.plugin import AuthPlugin
from infra.plugins.auth.service import AuthService

__all__ = ["AuthPlugin", "AuthService", "Principal"]
```

- [ ] **Step 5: Add auth plugin to built-ins**

Modify `infra/plugins/builtin.py` so it returns AI and auth:

```python
from infra.plugins.ai.plugin import AIPlugin
from infra.plugins.auth.plugin import AuthPlugin
from infra.plugins.contract import InfraPlugin


def get_builtin_plugins() -> list[InfraPlugin]:
    return [AIPlugin(), AuthPlugin()]
```

- [ ] **Step 6: Run auth tests**

Run: `pytest tests/plugins/test_auth_plugin.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add infra/plugins/auth infra/plugins/builtin.py tests/plugins/test_auth_plugin.py
git commit -m "feat: add minimal auth plugin"
```

### Task 7: Observability And Tasks Plugins

**Files:**
- Create: `infra/plugins/observability/__init__.py`
- Create: `infra/plugins/observability/plugin.py`
- Create: `infra/plugins/tasks/__init__.py`
- Create: `infra/plugins/tasks/models.py`
- Create: `infra/plugins/tasks/queue.py`
- Create: `infra/plugins/tasks/adapters/memory.py`
- Create: `infra/plugins/tasks/plugin.py`
- Modify: `infra/plugins/builtin.py`
- Test: `tests/plugins/test_observability_plugin.py`
- Test: `tests/plugins/test_tasks_plugin.py`

- [ ] **Step 1: Write failing observability and task tests**

Create `tests/plugins/test_observability_plugin.py`:

```python
import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.manager import PluginManager
from infra.plugins.observability.plugin import ObservabilityPlugin


@pytest.mark.asyncio
async def test_observability_exposes_health_snapshot_service():
    settings = InfraSettings(infra={"plugins": {"observability": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[ObservabilityPlugin()])

    await manager.startup()
    observability = manager.get("observability")
    snapshot = observability.health_snapshot()

    assert snapshot["observability"].status is HealthState.HEALTHY
```

Create `tests/plugins/test_tasks_plugin.py`:

```python
import pytest

from infra.config.models import InfraSettings
from infra.plugins.manager import PluginManager
from infra.plugins.tasks.plugin import TasksPlugin


@pytest.mark.asyncio
async def test_tasks_memory_queue_runs_registered_handler():
    settings = InfraSettings(
        infra={"plugins": {"tasks": {"enabled": True, "config": {"adapter": "memory"}}}}
    )
    manager = PluginManager(settings=settings, plugins=[TasksPlugin()])
    await manager.startup()
    tasks = manager.get("tasks")
    seen = []

    async def handler(payload):
        seen.append(payload)

    tasks.register_handler("email", handler)
    task_id = await tasks.enqueue("email", {"to": "a@example.com"})
    await tasks.drain_once()

    assert task_id.startswith("mem-")
    assert seen == [{"to": "a@example.com"}]
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/plugins/test_observability_plugin.py tests/plugins/test_tasks_plugin.py -v`

Expected: FAIL with missing plugin packages.

- [ ] **Step 3: Implement observability service and plugin**

Create `infra/plugins/observability/plugin.py`:

```python
from infra.core.health import HealthRegistry, HealthState
from infra.plugins.contract import PluginContext, PluginMetadata


class ObservabilityService:
    def __init__(self, health: HealthRegistry) -> None:
        self.health = health

    def health_snapshot(self):
        return self.health.snapshot()


class ObservabilityPlugin:
    metadata = PluginMetadata(
        name="observability",
        version="1.0.0",
        default_enabled=True,
        provides=["observability"],
    )
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["observability"] = ObservabilityService(health=ctx.services["_health"])

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("observability", HealthState.HEALTHY)
```

Update `PluginManager.__init__` to make the health registry available to plugins:

```python
self.services: dict[str, Any] = {"_health": self.health}
```

Create `infra/plugins/observability/__init__.py`:

```python
from infra.plugins.observability.plugin import ObservabilityPlugin, ObservabilityService

__all__ = ["ObservabilityPlugin", "ObservabilityService"]
```

- [ ] **Step 4: Implement memory task queue and plugin**

Create `infra/plugins/tasks/models.py`:

```python
from pydantic import BaseModel


class TaskEnvelope(BaseModel):
    task_id: str
    task_type: str
    payload: dict
```

Create `infra/plugins/tasks/adapters/memory.py`:

```python
from collections.abc import Awaitable, Callable

from infra.plugins.tasks.models import TaskEnvelope


TaskHandler = Callable[[dict], Awaitable[None]]


class MemoryTaskQueue:
    def __init__(self) -> None:
        self._queue: list[TaskEnvelope] = []
        self._handlers: dict[str, TaskHandler] = {}
        self._counter = 0

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler

    async def enqueue(self, task_type: str, payload: dict, idempotency_key: str | None = None) -> str:
        self._counter += 1
        task_id = idempotency_key or f"mem-{self._counter}"
        self._queue.append(TaskEnvelope(task_id=task_id, task_type=task_type, payload=payload))
        return task_id

    async def drain_once(self) -> None:
        pending = list(self._queue)
        self._queue.clear()
        for envelope in pending:
            handler = self._handlers[envelope.task_type]
            await handler(envelope.payload)
```

Create `infra/plugins/tasks/plugin.py`:

```python
from pydantic import BaseModel

from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.tasks.adapters.memory import MemoryTaskQueue


class TasksPluginConfig(BaseModel):
    adapter: str = "memory"


class TasksPlugin:
    metadata = PluginMetadata(
        name="tasks",
        version="1.0.0",
        default_enabled=False,
        provides=["tasks"],
    )
    config_model = TasksPluginConfig

    def register(self, ctx: PluginContext) -> None:
        config = TasksPluginConfig(**ctx.plugin_settings.config)
        if config.adapter != "memory":
            raise ValueError(f"unsupported task adapter: {config.adapter}")
        ctx.services["tasks"] = MemoryTaskQueue()

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("tasks", HealthState.HEALTHY)
```

Create `infra/plugins/tasks/__init__.py`:

```python
from infra.plugins.tasks.adapters.memory import MemoryTaskQueue
from infra.plugins.tasks.models import TaskEnvelope
from infra.plugins.tasks.plugin import TasksPlugin

__all__ = ["MemoryTaskQueue", "TaskEnvelope", "TasksPlugin"]
```

- [ ] **Step 5: Add plugins to built-ins**

Update `infra/plugins/builtin.py`:

```python
from infra.plugins.ai.plugin import AIPlugin
from infra.plugins.auth.plugin import AuthPlugin
from infra.plugins.contract import InfraPlugin
from infra.plugins.observability.plugin import ObservabilityPlugin
from infra.plugins.tasks.plugin import TasksPlugin


def get_builtin_plugins() -> list[InfraPlugin]:
    return [ObservabilityPlugin(), AIPlugin(), AuthPlugin(), TasksPlugin()]
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/plugins/test_observability_plugin.py tests/plugins/test_tasks_plugin.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add infra/plugins/observability infra/plugins/tasks infra/plugins/builtin.py tests/plugins/test_observability_plugin.py tests/plugins/test_tasks_plugin.py
git commit -m "feat: add observability and task plugins"
```

### Task 8: Peripheral Plugin Interfaces And Minimal Adapters

**Files:**
- Create: `infra/plugins/storage/`
- Create: `infra/plugins/webhooks/`
- Create: `infra/plugins/payment/`
- Create: `infra/plugins/ratelimit/`
- Create: `infra/plugins/notifications/`
- Modify: `infra/plugins/builtin.py`
- Test: `tests/plugins/test_peripheral_plugins.py`

- [ ] **Step 1: Write failing peripheral plugin tests**

Create `tests/plugins/test_peripheral_plugins.py`:

```python
import pytest

from infra.config.models import InfraSettings
from infra.plugins.manager import PluginManager
from infra.plugins.notifications.plugin import NotificationsPlugin
from infra.plugins.payment.plugin import PaymentPlugin
from infra.plugins.ratelimit.plugin import RateLimitPlugin
from infra.plugins.storage.plugin import StoragePlugin
from infra.plugins.webhooks.plugin import WebhooksPlugin


@pytest.mark.asyncio
async def test_storage_local_adapter_round_trip(tmp_path):
    settings = InfraSettings(
        infra={
            "plugins": {
                "storage": {
                    "enabled": True,
                    "config": {"adapter": "local", "base_path": str(tmp_path)},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[StoragePlugin()])
    await manager.startup()
    storage = manager.get("storage")

    await storage.put_object("a.txt", b"hello", content_type="text/plain")

    assert await storage.get_object("a.txt") == b"hello"
    assert await storage.exists("a.txt") is True


@pytest.mark.asyncio
async def test_webhook_dispatcher_calls_registered_handler():
    settings = InfraSettings(infra={"plugins": {"webhooks": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[WebhooksPlugin()])
    await manager.startup()
    webhooks = manager.get("webhooks")
    seen = []

    async def handler(event):
        seen.append(event)

    webhooks.register_handler("invoice.paid", handler)
    await webhooks.dispatch({"type": "invoice.paid", "data": {"id": "evt_1"}})

    assert seen == [{"type": "invoice.paid", "data": {"id": "evt_1"}}]


@pytest.mark.asyncio
async def test_payment_mock_provider_creates_checkout():
    settings = InfraSettings(infra={"plugins": {"payment": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[PaymentPlugin()])
    await manager.startup()
    payment = manager.get("payment")

    checkout = await payment.create_checkout(amount=1000, currency="usd")

    assert checkout["id"].startswith("mock-checkout-")
    assert checkout["amount"] == 1000


@pytest.mark.asyncio
async def test_ratelimit_memory_adapter_blocks_after_limit():
    settings = InfraSettings(infra={"plugins": {"ratelimit": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[RateLimitPlugin()])
    await manager.startup()
    limiter = manager.get("ratelimit")

    first = limiter.check("user-1", limit=1, window_seconds=60)
    second = limiter.check("user-1", limit=1, window_seconds=60)

    assert first.allowed is True
    assert second.allowed is False


@pytest.mark.asyncio
async def test_notifications_noop_records_delivery():
    settings = InfraSettings(infra={"plugins": {"notifications": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[NotificationsPlugin()])
    await manager.startup()
    notifications = manager.get("notifications")

    result = await notifications.notify("email", "a@example.com", {"subject": "hi"})

    assert result["status"] == "accepted"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/plugins/test_peripheral_plugins.py -v`

Expected: FAIL with missing peripheral plugin modules.

- [ ] **Step 3: Implement storage local adapter**

Create `infra/plugins/storage/plugin.py` with a `StoragePlugin` that registers `LocalStorage`. The local service must write under the configured `base_path`, create parent directories, and expose `put_object`, `get_object`, `delete_object`, `exists`, and `presign_url`.

Use this exact `LocalStorage` implementation in `infra/plugins/storage/local.py`:

```python
from pathlib import Path


class LocalStorage:
    def __init__(self, base_path: str) -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.base_path / key

    async def put_object(self, key: str, data: bytes, content_type: str | None = None) -> dict:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"key": key, "content_type": content_type}

    async def get_object(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    async def delete_object(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def presign_url(self, key: str, expires: int = 3600) -> str:
        return self._path(key).as_uri()
```

- [ ] **Step 4: Implement webhooks dispatcher**

Create `infra/plugins/webhooks/dispatcher.py`:

```python
from collections.abc import Awaitable, Callable


WebhookHandler = Callable[[dict], Awaitable[None]]


class WebhookDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[str, list[WebhookHandler]] = {}

    def register_handler(self, event_type: str, handler: WebhookHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def dispatch(self, event: dict) -> None:
        event_type = event["type"]
        for handler in self._handlers.get(event_type, []):
            await handler(event)
```

Wrap it in `WebhooksPlugin` with health status `healthy`.

- [ ] **Step 5: Implement payment mock, rate limit memory, and notifications no-op**

Use exact service behavior:

```python
class MockPaymentService:
    def __init__(self) -> None:
        self._counter = 0

    async def create_checkout(self, amount: int, currency: str) -> dict:
        self._counter += 1
        return {"id": f"mock-checkout-{self._counter}", "amount": amount, "currency": currency}
```

```python
from dataclasses import dataclass
import time


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int


class MemoryRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = time.time()
        hits = [hit for hit in self._hits.get(key, []) if now - hit < window_seconds]
        allowed = len(hits) < limit
        if allowed:
            hits.append(now)
        self._hits[key] = hits
        return RateLimitResult(allowed=allowed, remaining=max(0, limit - len(hits)))
```

```python
class NoopNotificationService:
    async def notify(self, channel: str, recipient: str, payload: dict) -> dict:
        return {
            "status": "accepted",
            "channel": channel,
            "recipient": recipient,
            "payload": payload,
        }
```

Each service gets a plugin wrapper with `register`, no-op startup/shutdown, and healthy status.

- [ ] **Step 6: Export packages and add built-ins**

Each peripheral plugin package must have an `__init__.py` exporting the plugin class and service class. Add all peripheral plugin classes to `get_builtin_plugins()`.

- [ ] **Step 7: Run tests**

Run: `pytest tests/plugins/test_peripheral_plugins.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add infra/plugins/storage infra/plugins/webhooks infra/plugins/payment infra/plugins/ratelimit infra/plugins/notifications infra/plugins/builtin.py tests/plugins/test_peripheral_plugins.py
git commit -m "feat: add peripheral plugin interfaces"
```

### Task 9: Optional Dependencies, Docs, And Examples

**Files:**
- Modify: `pyproject.toml`
- Rewrite: `README.md`
- Create: `docs/architecture.md`
- Create: `docs/plugins.md`
- Create: `docs/ai.md`
- Rewrite: `examples/minimal/app.py`
- Create: `examples/ai_app/app.py`
- Create: `examples/full_stack/app.py`
- Test: `tests/examples/test_examples_import.py`

- [ ] **Step 1: Write failing example import tests**

Create `tests/examples/test_examples_import.py`:

```python
import importlib.util
from pathlib import Path


def import_file(path: str):
    file_path = Path(path)
    spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_minimal_example_imports():
    module = import_file("examples/minimal/app.py")
    assert hasattr(module, "app")


def test_ai_example_imports():
    module = import_file("examples/ai_app/app.py")
    assert hasattr(module, "app")
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/examples/test_examples_import.py -v`

Expected: FAIL until examples are rewritten.

- [ ] **Step 3: Split optional dependencies in `pyproject.toml`**

Set core dependencies to FastAPI, Starlette, Uvicorn, Pydantic, Pydantic Settings, and Loguru. Move provider and backend packages into extras:

```toml
[project.optional-dependencies]
mysql = ["aiomysql>=0.2.0,<0.3.0"]
redis = ["redis>=6.4.0,<7.0.0"]
ai-openai = ["openai>=1.0.0"]
ai-anthropic = ["anthropic>=0.40.0"]
ai-gemini = ["google-genai>=1.0.0"]
ai = ["openai>=1.0.0", "anthropic>=0.40.0", "google-genai>=1.0.0"]
tasks-redis = ["redis>=6.4.0,<7.0.0"]
observability = ["prometheus-client>=0.19.0", "opentelemetry-api>=1.20.0", "opentelemetry-sdk>=1.20.0"]
dev = ["pytest>=8.0.0", "pytest-asyncio>=0.25.0", "pytest-cov>=4.1.0", "black>=23.11.0", "isort>=5.12.0", "mypy>=1.7.0", "anyio>=4.0.0"]
```

- [ ] **Step 4: Rewrite minimal example**

Make `examples/minimal/app.py` contain:

```python
from fastapi import FastAPI

from infra import InfraSettings, setup_infra

app = FastAPI(title="fastapi-infra minimal")
infra = setup_infra(app, InfraSettings())


@app.get("/")
async def root():
    return {"ok": True}
```

- [ ] **Step 5: Add AI example**

Create `examples/ai_app/app.py`:

```python
from fastapi import FastAPI

from infra import InfraSettings, setup_infra

settings = InfraSettings(
    infra={
        "plugins": {
            "ai": {
                "enabled": True,
                "config": {"default_provider": "mock", "providers": {"mock": {}}},
            }
        }
    }
)
app = FastAPI(title="fastapi-infra ai")
infra = setup_infra(app, settings)


@app.post("/chat")
async def chat(payload: dict):
    ai = infra.get("ai")
    response = await ai.chat_text(payload["message"])
    return response.model_dump()
```

- [ ] **Step 6: Write docs**

`README.md` must show only the new public entry point. `docs/architecture.md` must describe core versus plugins. `docs/plugins.md` must describe `InfraPlugin` and feature flags. `docs/ai.md` must describe mock, OpenAI, Anthropic, and Gemini provider configuration.

- [ ] **Step 7: Run example tests**

Run: `pytest tests/examples/test_examples_import.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml README.md docs examples tests/examples/test_examples_import.py
git commit -m "docs: update plugin platform docs and examples"
```

### Task 10: Full Verification And Integration Cleanup

**Files:**
- Modify as needed: files touched by previous tasks.
- Test: all tests.

- [ ] **Step 1: Run no-business-import scan**

Run: `pytest tests/test_no_business_imports.py -v`

Expected: PASS.

- [ ] **Step 2: Run core tests**

Run: `pytest tests/core -v`

Expected: PASS.

- [ ] **Step 3: Run plugin tests**

Run: `pytest tests/plugins -v`

Expected: PASS.

- [ ] **Step 4: Run example tests**

Run: `pytest tests/examples -v`

Expected: PASS.

- [ ] **Step 5: Run full test suite**

Run: `pytest -v`

Expected: PASS.

- [ ] **Step 6: Run import smoke**

Run:

```bash
python - <<'PY'
from fastapi import FastAPI
from infra import InfraSettings, setup_infra

app = FastAPI()
infra = setup_infra(app, InfraSettings())
print(type(infra).__name__)
PY
```

Expected output contains:

```text
InfraContext
```

- [ ] **Step 7: Inspect git status**

Run: `git status --short`

Expected: only intentional source, test, docs, and example changes are present. Runtime artifacts such as `logs/*.log` and `__pycache__/` must not be staged.

- [ ] **Step 8: Commit final cleanup**

```bash
git add infra tests docs examples README.md pyproject.toml
git commit -m "test: verify plugin platform integration"
```

## Self-Review Notes

Spec coverage:

- Core kernel: Tasks 1-3.
- Breaking cleanup and no compatibility layer: Task 4.
- AI plugin with mock/OpenAI/Anthropic/Gemini SDK adapter boundary: Task 5.
- Auth, observability, tasks: Tasks 6-7.
- Storage, webhooks, payment, ratelimit, notifications: Task 8.
- Docs, examples, optional dependencies: Task 9.
- Final verification: Task 10.

Type consistency:

- Public settings type is `InfraSettings`.
- Public setup entry point is `setup_infra(app, settings)`.
- Runtime context is `InfraContext`.
- Plugin state type is `HealthStatus` with `HealthState`.
- Services are retrieved through `infra.get(name)` and plugin manager `get(name)`.

Execution preference:

- Use subagent-driven development for this plan because the file ownership is naturally parallel and the user explicitly permits parallel agent work.
