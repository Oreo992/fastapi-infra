# FastAPI Infra Plugin Platform Design

Date: 2026-05-05
Status: Approved for implementation planning

## Purpose

`fastapi-infra` should become a reusable infrastructure layer for future FastAPI
projects. A new project should be able to copy or install this package, enable
only the needed infrastructure components, and start building business features
without rewriting connection management, lifecycle hooks, AI provider plumbing,
authentication boundaries, task queues, storage, payment integration scaffolding,
or observability foundations.

The package has not been publicly released, so this design intentionally chooses
a breaking cleanup over compatibility. There should be one clean public API for
each capability. Old helper functions, duplicated entry points, and compatibility
facades are not part of the target design.

## Current State

The repository already contains useful pieces: settings, HTTP client, database
manager, Redis cache, logging, service registry, middleware, Redis Streams, and
application lifecycle callbacks.

The current code is not yet a clean reusable platform:

- `infra/startup/*` still contains original business imports such as `app.core`
  and `app.services`.
- `DatabaseManager` couples MySQL and Redis initialization, so projects cannot
  enable one without the other.
- Several modules rely on process-wide singletons and import-time side effects.
- Some errors and contracts still contain business-specific concepts such as
  personality-test/session/question/workflow errors.
- README and examples reference APIs and directories that do not match the real
  code.
- There is no real plugin contract, feature-flag model, dependency resolution,
  or plugin health aggregation.

## Design Goals

1. Provide a small, stable kernel for configuration, lifecycle, plugin loading,
   service registration, health checks, logging setup, and error contracts.
2. Move optional infrastructure capabilities into plugins that can be enabled,
   disabled, or auto-detected.
3. Make the code AI-friendly: explicit protocols, Pydantic DTOs, clear module
   boundaries, direct examples, and no hidden compatibility paths.
4. Support parallel implementation by giving each plugin a clear interface and
   ownership boundary.
5. Keep vendor-specific code outside the core kernel.

## Non-Goals

- Backward compatibility with v0.1 APIs.
- Compatibility with old routes or old examples.
- Recreating official AI SDK behavior through hand-written HTTP clients.
- Fully implementing every payment channel, every ASR/TTS vendor, or every
  queue backend in the first implementation batch.
- Keeping business-specific startup callbacks, exceptions, or service names in
  the infrastructure package.

## Architecture

The public setup path should be singular:

```python
from fastapi import FastAPI
from infra import InfraSettings, setup_infra

settings = InfraSettings()
app = FastAPI()
infra = setup_infra(app, settings)

ai = infra.get("ai")
```

The target architecture:

```text
business FastAPI app
  -> setup_infra(app, settings)
    -> InfraContext
      -> PluginManager
        -> ai
        -> auth
        -> observability
        -> tasks
        -> storage
        -> webhooks
        -> payment
        -> ratelimit
        -> notifications
```

The core kernel owns only:

- Settings and plugin configuration parsing.
- Feature flags.
- Plugin discovery and dependency ordering.
- Plugin lifecycle.
- Service registry.
- Health registry.
- Common errors and API contracts.
- Explicit logging setup.
- FastAPI integration hooks.

The kernel must not import provider SDKs, Redis, MySQL, payment SDKs, storage SDKs,
or business modules unless a plugin explicitly enables them.

## Proposed Layout

```text
infra/
  __init__.py
  core/
    app.py
    context.py
    flags.py
    health.py
  config/
    settings.py
    models.py
  plugins/
    contract.py
    manager.py
    builtin.py
    ai/
    auth/
    observability/
    tasks/
    storage/
    webhooks/
    payment/
    ratelimit/
    notifications/
  registry/
  lifecycle/
  http/
  common/
  exceptions/
tests/
  core/
  plugins/
examples/
  minimal/
  ai_app/
  full_stack/
docs/
  architecture.md
  plugins.md
  ai.md
```

The old `infra/startup/` package should either be replaced by a generic
`infra/lifecycle/` package or reduced to a single generic lifecycle module. Files
that import `app.*` should be removed from the distributable package.

## Plugin Contract

Every plugin implements the same contract:

```python
class InfraPlugin(Protocol):
    metadata: PluginMetadata
    config_model: type[BaseModel] | None

    def register(self, ctx: PluginContext) -> None: ...
    async def startup(self, ctx: PluginContext) -> None: ...
    async def shutdown(self, ctx: PluginContext) -> None: ...
    async def health_check(self, ctx: PluginContext) -> HealthStatus: ...
```

`PluginMetadata` contains:

- `name`: stable plugin name, such as `ai` or `payment`.
- `version`: plugin version.
- `dependencies`: required plugin names.
- `optional_dependencies`: Python packages used by enabled adapters.
- `default_enabled`: default feature flag.
- `provides`: service names registered into the context registry.

`PluginContext` is the only shared infrastructure object a plugin receives. It
contains:

- FastAPI app.
- Infra settings.
- Service registry.
- Health registry.
- Lifecycle manager.
- Logger provider.
- Plugin manager.

Plugins must not import each other directly. They declare dependencies and
retrieve services through the context registry.

## Feature Flags

Plugin enablement uses a three-state flag:

```python
enabled: bool | None
```

The semantics are:

- `true`: force-enable the plugin. Missing required config or package dependencies
  fails startup with a readable configuration error.
- `false`: fully disable the plugin. The plugin should not register services,
  initialize resources, or import heavy optional dependencies.
- `null`: auto mode. The manager enables the plugin only when required config and
  dependencies are present. Otherwise it records a disabled/skipped status with
  the reason.

The plugin manager resolves dependencies before startup. Startup order follows
dependencies; shutdown order is reversed.

## Configuration Model

Configuration is namespaced under `infra.plugins`:

```yaml
infra:
  plugins:
    ai:
      enabled: true
      default_provider: openai
      providers:
        openai:
          api_key: ${OPENAI_API_KEY}
        anthropic:
          api_key: ${ANTHROPIC_API_KEY}
        gemini:
          api_key: ${GEMINI_API_KEY}

    auth:
      enabled: true

    payment:
      enabled: false

    tasks:
      enabled: null
```

Each plugin owns a Pydantic config model. `InfraSettings` aggregates plugin
settings and passes the validated plugin-specific config to the plugin. Long-term
configuration should not be a flat set of arbitrary fields.

## Health Model

Health checks should return structured status, not only booleans:

```python
class HealthStatus(BaseModel):
    name: str
    status: Literal["healthy", "degraded", "unhealthy", "disabled"]
    message: str | None = None
    details: dict[str, Any] = {}
```

The health registry aggregates:

- Core status.
- Plugin enablement status.
- Plugin startup status.
- Runtime health check results.

Disabled plugins are represented explicitly as `disabled`, not as failed checks.

## Plugin Boundaries

### AI Plugin

Priority: P0. First implementation batch.

The AI plugin provides a unified provider registry and protocol for:

- `chat`
- `stream_chat`
- tools/function calling

Initial providers:

- `mock`: deterministic tests and no-external-service examples.
- `openai`: official OpenAI SDK.
- `anthropic`: official Anthropic SDK.
- `gemini`: official Google Gemini SDK.

The plugin should not reimplement SDK HTTP details. Provider adapters translate
between infra DTOs and the official SDK request/response types.

DeepSeek is not a first-batch explicit adapter. It can later be supported through
an OpenAI-compatible adapter or a dedicated provider if needed.

The first AI implementation does not include embedding, ASR, TTS, image
generation, or vendor-specific agent runtimes.

### Auth Plugin

Priority: P0. First implementation batch includes interface and minimal useful
implementation.

The auth plugin provides:

- `Principal`
- `authenticate(request) -> Principal`
- `require_scopes(scopes)`
- API key validation
- JWT validation abstraction

It must not bind to a user table. Business applications own user persistence and
authorization policy beyond the reusable boundary.

### Observability Plugin

Priority: P0. First implementation batch.

The observability plugin provides:

- Health endpoint helpers.
- Plugin status reporting.
- Metric facade.
- Trace context integration with logging.
- Optional Prometheus and OpenTelemetry extras.

The default implementation should work without Prometheus or OpenTelemetry
installed.

### Tasks Plugin

Priority: P0. First implementation batch includes a facade and one adapter.

The tasks plugin provides:

- `enqueue(task_type, payload, idempotency_key=None)`
- `register_handler(task_type, handler)`
- `start_worker()`
- `stop_worker()`
- `get_stats()`

The default adapter is Redis Streams. Celery, Kafka, and NATS are later adapters,
not core dependencies.

### Storage Plugin

Priority: P1. Interface plus local adapter in the first platform batch.

The storage plugin provides:

- `put_object`
- `get_object`
- `delete_object`
- `presign_url`
- `exists`

The first adapter is local filesystem storage for tests and small projects.
S3-compatible storage can be added later.

### Webhooks Plugin

Priority: P1. Interface plus inbound event dispatch.

The webhooks plugin provides:

- Signature verification.
- Event dispatch.
- Handler registration.
- Idempotency support.

Outbound webhook delivery is a later adapter unless it is needed by another first
batch plugin.

### Payment Plugin

Priority: P1/P2. Interface and mock provider in the first platform batch.

The payment plugin provides:

- `create_checkout`
- `verify_webhook`
- `refund`
- `get_payment_status`

Real Stripe, Alipay, and WeChat Pay providers should not be rushed into the first
implementation batch. Payment state machines, idempotency, audit trails, webhook
security, and compliance deserve a separate implementation plan.

### Rate Limit Plugin

Priority: P1. Interface plus memory or Redis adapter.

The ratelimit plugin provides:

- Middleware integration.
- Dependency integration.
- `check(key, limit, window) -> RateLimitResult`.

Redis and in-memory adapters are valid. In-memory is enough for tests and single
process examples.

### Notifications Plugin

Priority: P2. Interface and no-op/mock implementation in the first platform
batch.

The notifications plugin is an aggregation layer over email, SMS, and webhook
channels. Real provider adapters can follow after webhooks and tasks are stable.

## Breaking Cleanup

The implementation should remove compatibility pressure from the design:

- Remove old compatibility functions that duplicate the new public API.
- Remove business-specific exceptions and error codes.
- Remove `from app.` imports from distributable infra modules.
- Replace global import-time logger setup with explicit infra setup.
- Split MySQL, Redis, cache, streams, and task queue responsibilities.
- Rewrite README and examples after the new public API lands.
- Delete examples that do not smoke test.

## Tests and Verification

Required tests:

- Plugin manager contract tests with fake plugins.
- Feature flag tests for `true`, `false`, and `null`.
- Missing dependency tests that confirm disabled plugins do not import optional
  packages.
- Dependency ordering tests for startup and shutdown.
- Health registry tests for healthy, degraded, unhealthy, and disabled states.
- AI mock provider tests for chat, stream chat, and tools/function calling.
- SDK adapter boundary tests using mocks for OpenAI, Anthropic, and Gemini.
- Auth minimal tests for API key/JWT boundaries.
- Task facade tests using fake or local adapter.
- Import smoke tests proving no `app.*` import remains.
- Example smoke tests for `examples/minimal` and `examples/ai_app`.

Real AI provider integration tests should be skipped unless the relevant API key
environment variables are present.

## Parallel Implementation Strategy

Implementation can be split across sub agents after the implementation plan is
written:

- Agent 1: core kernel, settings, feature flags, plugin manager, health registry.
- Agent 2: breaking cleanup, public API cleanup, exceptions, contracts.
- Agent 3: AI plugin protocol, mock provider, OpenAI/Anthropic/Gemini SDK adapters.
- Agent 4: auth, observability, and tasks minimal plugins.
- Agent 5: storage, webhooks, payment, ratelimit, and notifications interfaces
  plus mock/local adapters.
- Agent 6: tests, docs, examples, smoke verification.

The parent agent owns integration, conflict resolution, final verification, and
user-facing summary.

## Acceptance Criteria

The design is implemented when:

- `setup_infra(app, settings)` is the single recommended public entry point.
- Plugins can be enabled, disabled, or auto-detected through config.
- Disabled plugins do not initialize or import heavy optional dependencies.
- Plugin startup and shutdown order respects declared dependencies.
- Health checks expose core and plugin states.
- AI plugin supports mock, OpenAI, Anthropic, and Gemini providers through official
  SDK adapters for chat, stream chat, and tools/function calling.
- First-batch plugin interfaces are present and documented.
- The repository has meaningful tests under `tests/`.
- Examples use only the new public API.
- There are no distributable infra modules importing `app.*`.
- There are no compatibility facades for removed v0.1 APIs.
