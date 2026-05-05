# P0 Runtime Pluginization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the next P0 slice by moving core runtime startup to FastAPI lifespan and bringing legacy database/cache/http/task/observability capabilities behind optional plugins.

**Architecture:** Keep `infra` top-level public API small. Optional external dependencies must be loaded lazily inside plugin startup/register paths so a core install can still `import infra`. Built-in plugins may include optional plugins only when their module can be imported without importing optional SDK/backend packages; network services must default to disabled or no-connect unless explicitly enabled.

**Tech Stack:** Python 3.11+, FastAPI lifespan, Pydantic v2, pytest, optional aiomysql/redis/aiohttp/orjson extras.

---

## File Map

- Modify `infra/core/app.py`: replace `on_event` hooks with lifespan composition.
- Modify `infra/core/context.py`: keep `InfraContext` API stable.
- Modify `tests/core/test_setup_infra.py`: assert no `on_event` warning path and previous lifespan composition.
- Create `infra/plugins/database/`: optional `DatabasePlugin`, `DatabasePluginConfig`, lazy `DatabaseManager` service registration.
- Create `infra/plugins/cache/`: optional `CachePlugin`, lazy `CacheService` service registration.
- Create `infra/plugins/http/`: optional `HTTPPlugin`, lazy `HttpClient` service registration and shutdown.
- Modify `infra/plugins/builtin.py`: include optional database/cache/http plugins with `default_enabled=False`.
- Create `tests/plugins/test_backend_plugins.py`: optional plugin disabled/enabled behavior with monkeypatched fake services.
- Create `infra/plugins/tasks/adapters/redis_stream.py`: Redis Streams adapter with injected fake client support.
- Modify `infra/plugins/tasks/plugin.py`: config-driven memory or redis adapter selection.
- Modify `tests/plugins/test_tasks_plugin.py`: adapter selection and Redis fake-client behavior.
- Create `infra/plugins/observability/routes.py`: helper to install `/healthz`, `/readyz`, `/metrics` routes.
- Modify `infra/plugins/observability/__init__.py`: export route helper.
- Create `tests/plugins/test_observability_routes.py`: route helper behavior.
- Update `README.md`, `docs/architecture.md`, `docs/plugins.md`: document optional backend plugins and lifespan.

## Task 1: Lifespan-Based Setup

**Files:**
- Modify: `infra/core/app.py`
- Test: `tests/core/test_setup_infra.py`

- [ ] **Step 1: Add failing tests**

Add tests that create `FastAPI(lifespan=...)`, call `setup_infra()`, use `TestClient`, and assert both the previous lifespan and infra startup/shutdown run. Add a test that `setup_infra()` does not append handlers to `app.router.on_startup` or `app.router.on_shutdown`.

- [ ] **Step 2: Verify red**

Run:

```bash
pytest tests/core/test_setup_infra.py -v
```

Expected before implementation: failure because `setup_infra()` still appends `on_event` handlers and emits deprecation warnings.

- [ ] **Step 3: Implement lifespan composition**

Implement `setup_infra()` with `contextlib.asynccontextmanager`. Capture `previous_lifespan = app.router.lifespan_context`, then set a new lifespan context that starts infra before entering the previous lifespan, yields, exits previous lifespan, then shuts infra down.

- [ ] **Step 4: Verify**

Run:

```bash
pytest tests/core/test_setup_infra.py tests/core/test_plugin_manager.py -v
```

- [ ] **Step 5: Commit**

```bash
git add infra/core/app.py tests/core/test_setup_infra.py
git commit -m "refactor: use lifespan for infra setup"
```

## Task 2: Backend Service Plugins

**Files:**
- Create: `infra/plugins/database/__init__.py`
- Create: `infra/plugins/database/plugin.py`
- Create: `infra/plugins/cache/__init__.py`
- Create: `infra/plugins/cache/plugin.py`
- Create: `infra/plugins/http/__init__.py`
- Create: `infra/plugins/http/plugin.py`
- Modify: `infra/plugins/builtin.py`
- Test: `tests/plugins/test_backend_plugins.py`

- [ ] **Step 1: Add failing tests**

Tests must verify:
- builtins include `database`, `cache`, and `http` plugin names;
- these plugins are disabled by default and do not register services unless enabled;
- when enabled with monkeypatched fake classes, services `database`, `cache`, and `http` are registered;
- `HTTPPlugin.shutdown()` closes the registered client if it has an async `close()`.

- [ ] **Step 2: Verify red**

Run:

```bash
pytest tests/plugins/test_backend_plugins.py -v
```

- [ ] **Step 3: Implement plugins**

Each plugin must lazy import legacy implementation inside `register()` only. Use config models:
- `DatabasePluginConfig(config: dict = {}, connect_on_startup: bool = False)`
- `CachePluginConfig(namespace: str = "", database_service: str = "database")`
- `HTTPPluginConfig(base_url: str = "", timeout: float = 30.0, headers: dict[str, str] = {})`

Set plugin metadata `default_enabled=False`. Register services by exact names `database`, `cache`, and `http`.

- [ ] **Step 4: Verify**

Run:

```bash
pytest tests/plugins/test_backend_plugins.py tests/plugins/test_builtin_plugins.py -v
```

- [ ] **Step 5: Commit**

```bash
git add infra/plugins/database infra/plugins/cache infra/plugins/http infra/plugins/builtin.py tests/plugins/test_backend_plugins.py
git commit -m "feat: add optional backend plugins"
```

## Task 3: Redis Streams Task Adapter

**Files:**
- Create: `infra/plugins/tasks/adapters/redis_stream.py`
- Modify: `infra/plugins/tasks/plugin.py`
- Modify: `infra/plugins/tasks/__init__.py`
- Test: `tests/plugins/test_tasks_plugin.py`

- [ ] **Step 1: Add failing tests**

Add tests for `RedisStreamTaskQueue` using an injected fake Redis client. Cover `enqueue()`, `dequeue()`, `complete()`, `fail()`, and `get()`. Add `TasksPlugin` config tests for `adapter="memory"` and `adapter="redis"`.

- [ ] **Step 2: Verify red**

Run:

```bash
pytest tests/plugins/test_tasks_plugin.py -v
```

- [ ] **Step 3: Implement adapter**

`RedisStreamTaskQueue` should accept `redis`, `stream_name="infra:tasks"`, and `consumer_group="infra"` arguments. It should use JSON payloads and keep the same public method shapes as `MemoryTaskQueue`.

- [ ] **Step 4: Verify**

Run:

```bash
pytest tests/plugins/test_tasks_plugin.py -v
```

- [ ] **Step 5: Commit**

```bash
git add infra/plugins/tasks tests/plugins/test_tasks_plugin.py
git commit -m "feat: add redis stream task adapter"
```

## Task 4: Observability Routes

**Files:**
- Create: `infra/plugins/observability/routes.py`
- Modify: `infra/plugins/observability/__init__.py`
- Test: `tests/plugins/test_observability_routes.py`

- [ ] **Step 1: Add failing tests**

Add `TestClient` tests for helper-installed `/healthz`, `/readyz`, and `/metrics`. Health/readiness should read from an `InfraContext` or compatible object with `health.snapshot()`.

- [ ] **Step 2: Verify red**

Run:

```bash
pytest tests/plugins/test_observability_routes.py -v
```

- [ ] **Step 3: Implement route helper**

Add `install_observability_routes(app, infra, prefix="")`. `/healthz` returns all statuses. `/readyz` returns 503 if any status is `unhealthy`, otherwise 200. `/metrics` returns a simple text exposition from observability counters/timers when service exists.

- [ ] **Step 4: Verify**

Run:

```bash
pytest tests/plugins/test_observability_routes.py -v
```

- [ ] **Step 5: Commit**

```bash
git add infra/plugins/observability tests/plugins/test_observability_routes.py
git commit -m "feat: add observability route helpers"
```

## Task 5: Docs And Final Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/plugins.md`

- [ ] **Step 1: Update docs**

Document lifespan setup, optional backend plugins, Redis task adapter, and observability routes.

- [ ] **Step 2: Run verification**

Run:

```bash
pytest tests/core -v
pytest tests/plugins -v
pytest -v
python -c "from fastapi import FastAPI; from infra import InfraSettings, setup_infra; app = FastAPI(); infra = setup_infra(app, InfraSettings()); print(type(infra).__name__)"
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/architecture.md docs/plugins.md
git commit -m "docs: document p0 runtime plugins"
```

## Self-Review

- This plan intentionally does not implement real JWT/OAuth, Stripe/Alipay/WeChat, S3/R2/GCS, ASR/TTS, or full OpenTelemetry exporters. Those are separate provider-plugin batches.
- Optional dependency plugins must not make `import infra` import aiomysql, redis, aiohttp, or orjson.
- Existing runtime artifacts such as `logs/app.log` and `__pycache__` must not be staged.
