# Architecture

FastAPI Infra is split into a small core and optional plugins.

## Core

The core package exposes four public entry points:

- `InfraSettings`
- `PluginSettings`
- `InfraContext`
- `setup_infra`

`setup_infra(app, settings)` attaches an `InfraContext` to `app.state.infra`, registers startup and shutdown hooks, starts configured plugins, and keeps a shared `HealthRegistry`.

The core does not know application business concepts. It only manages:

- settings and tri-state plugin flags
- plugin dependency ordering
- lifecycle startup and shutdown
- service registry access
- health snapshots

## Plugins

Plugins provide optional capabilities such as AI, auth, tasks, storage, payment, notifications, webhooks, rate limiting, and observability.

Each plugin owns its implementation and registers one or more named services into the plugin context. Failed startup rolls back service state so partially initialized services are not visible to the app.

## Defaults

`get_builtin_plugins()` returns the first-batch built-in plugins. Their default implementations are memory-safe or mock implementations, so a new project can start with no external database, Redis, payment provider, or AI key.
