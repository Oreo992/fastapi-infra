# Architecture

FastAPI Infra is split into a small core and optional plugins.

## Core

The core package exposes four public entry points:

- `InfraSettings`
- `PluginSettings`
- `InfraContext`
- `setup_infra`

`setup_infra(app, settings)` attaches an `InfraContext` to `app.state.infra`,
wraps the FastAPI lifespan, starts configured plugins before the app lifespan
enters, shuts them down after the app lifespan exits, and keeps a shared
`HealthRegistry`.

The core does not know application business concepts. It only manages:

- settings and tri-state plugin flags
- plugin dependency ordering
- lifecycle startup and shutdown
- service registry access
- health snapshots

## Plugins

Plugins provide optional capabilities such as AI, auth, database, cache, HTTP,
tasks, storage, payment, notifications, webhooks, rate limiting, and
observability.

Each plugin owns its implementation and registers one or more named services into
the plugin context. Failed startup rolls back service state so partially
initialized services are not visible to the app. Repeated startup before
shutdown is rejected, and shutdown failures keep plugin state available for a
cleanup retry.

## Defaults

`get_builtin_plugins()` returns the built-in plugins. The default-enabled
plugins use memory-safe or mock implementations, so a new project can start with
no external database, Redis, payment provider, or AI key. Backend plugins for
database, cache, and HTTP clients are included but default-disabled; forcing them
on requires installing their optional dependencies.
