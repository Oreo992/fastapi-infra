# Architecture

FastAPI Infra is split into a small core and optional plugins.

## Core

The core package exposes four public entry points:

- `InfraSettings`
- `PluginSettings`
- `InfraContext`
- `setup_infra`

`setup_infra(app, settings, health_check_timeout_seconds=5)` attaches an
`InfraContext` to `app.state.infra`, wraps the FastAPI lifespan, starts
configured plugins before the app lifespan enters, shuts them down after the app
lifespan exits, and keeps a shared `HealthRegistry`.

The core does not know application business concepts. It only manages:

- settings and tri-state plugin flags
- plugin dependency ordering
- lifecycle startup and shutdown
- service registry access
- health snapshots

## Plugins

Plugins provide optional capabilities such as AI, speech, auth, database, cache,
HTTP, tasks, storage, payment, notifications, webhooks, rate limiting, and
observability. Provider-backed plugins, such as AI, speech, and payment, expose a
stable service while letting individual providers remain replaceable behind the
plugin boundary.

Each plugin owns its implementation and registers one or more named services into
the plugin context. Failed startup rolls back service state so partially
initialized services are not visible to the app. Repeated startup before
shutdown is rejected, startup health checks time out by default after five
seconds, and shutdown failures keep plugin state available for a cleanup retry.

## Cross-Cutting Runtime Primitives

Some reusable runtime primitives intentionally remain below the plugin layer.
`HttpClient` owns connection pooling and optional HTTP-specific retry policy.
`TransactionCoordinator` provides Saga-style orchestration with explicit result,
failure, and compensation reports. `DistributedLockManager` provides Redis lease
locks with token-checked release and extension. These primitives require
callers to pass dependencies explicitly, so application code can compose them
without hidden process-wide singletons.

`RequestLoggingMiddleware` and `ErrorHandlingMiddleware` provide request-level
runtime behavior without owning application routes. Request logging accepts
incoming `X-Trace-ID`, `X-Request-ID`, and `X-Correlation-ID` headers, writes the
trace/request identifiers to `request.state`, mirrors them onto responses, and
does not log request bodies unless explicitly configured. Error handling formats
infra `AppException` subclasses and unexpected errors as `ApiResponse` payloads
with the same trace headers. `install_error_handlers()` adds the matching
FastAPI exception handlers for route-level `HTTPException` and request
validation errors, which are normally handled before middleware sees them.
`SecurityHeadersMiddleware` adds conservative response headers such as
`X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`,
`Permissions-Policy`, and optional HSTS.
`CORSMiddleware` defaults to non-credentialed public CORS and requires explicit
origins when credentials are enabled, avoiding the unsafe wildcard credentials
combination.

## Defaults

`get_builtin_plugins()` returns the built-in plugins, but the built-ins are
default-disabled. A new project starts with only the core lifecycle, settings,
service registry, and health registry. Enable each capability explicitly through
`InfraSettings`, and install the matching optional dependency extra when a
plugin needs one.
