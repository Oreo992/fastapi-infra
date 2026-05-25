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

Production release hooks are optional contracts, not mandatory `InfraPlugin`
methods. Implement only the hooks your plugin needs:

```python
class PluginReleaseCheckHook(Protocol):
    def release_check(self, settings: InfraSettings, config: Any) -> Iterable[object] | None: ...


class PluginReleaseDependencyHook(Protocol):
    def release_dependencies(
        self,
        settings: InfraSettings,
        config: Any,
    ) -> Iterable[PluginReleaseDependency] | None: ...


class PluginProviderPolicyHook(Protocol):
    def provider_release_policies(
        self,
        settings: InfraSettings,
        config: Any,
    ) -> Iterable[PluginProviderPolicy] | None: ...


class PluginProviderCertificationHook(Protocol):
    def provider_certifications(
        self,
        settings: InfraSettings,
        config: Any,
    ) -> Iterable[PluginProviderCertification] | None: ...


class PluginManifestHintsHook(Protocol):
    manifest_hints: Mapping[str, Any] | PluginManifestHints


class PluginConfigValidatorHook(Protocol):
    def validate_config(self, config: Any) -> None: ...
```

## Metadata

`PluginMetadata` declares:

- `name`: stable plugin id used by settings and health.
- `version`: plugin implementation version.
- `dependencies`: other plugin names that must be active first.
- `optional_dependencies`: importable packages required only when forced on.
- `default_enabled`: boolean default used when settings leave `enabled=None`.
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
- `enabled=None`: use the plugin metadata default. Built-in plugins default to disabled.

## Settings Loading

`load_infra_settings()` is exported from `infra.config` for apps that want file
and environment based configuration without importing extra packages:

```python
from infra.config import load_infra_settings

settings = load_infra_settings("infra.toml")
```

JSON and TOML files map directly to `InfraSettings`:

```toml
[infra.plugins.payment]
enabled = false

[infra.plugins.auth.config]
jwt_secret = "change-me"
```

Environment variables use double-underscore paths and override file values:

```bash
INFRA__INFRA__PLUGINS__PAYMENT__ENABLED=false
INFRA__INFRA__PLUGINS__AUTH__CONFIG__JWT_SECRET=change-me
```

Values are parsed as JSON when possible, so `true`, `false`, `null`, numbers,
arrays, and objects keep their structured types.

Configuration files can reference process environment variables for secrets.
The loader resolves the reference before plugin config validation and fails if
the variable is missing:

```toml
[infra.plugins.payment]
enabled = true

[infra.plugins.payment.config]
default_provider = "stripe"

[infra.plugins.payment.config.providers.stripe]
api_key = { "$env" = "STRIPE_API_KEY" }
webhook_secret = { "$env" = "STRIPE_WEBHOOK_SECRET" }
```

`{"$env": "NAME"}` is intentionally small: it only reads required environment
variables and does not support defaults. Optional values should stay absent from
the config or be provided with normal environment overrides.

## Services

Plugins write services to `ctx.services` during `register()`.

```python
def register(self, ctx: PluginContext) -> None:
    ctx.services["my_service"] = MyService()
```

Services are committed to the global registry only after the plugin starts and
passes health checks. Startup health checks have a default five-second timeout,
so a slow external probe fails startup instead of blocking the app indefinitely.
Pass `health_check_timeout_seconds` to `setup_infra()` when an application needs
a different startup probe budget.
This keeps failed plugins from leaking half-initialized services.

FastAPI routes can inject any registered service with the core dependency
helper:

```python
from typing import Annotated

from fastapi import Depends
from infra import infra_service
from infra.plugins import PAYMENT_SERVICE


@app.get("/billing")
async def billing(payment: Annotated[object, Depends(infra_service(PAYMENT_SERVICE))]):
    return {"enabled": True}
```

`infra_service("name")` still accepts raw service names for quick usage, but the
built-in service keys from `infra.plugins` are preferred for reusable application
code because they preserve the expected service type. Use `ServiceKey[T]` for
application-owned services. Pass `default=...` for optional service usage.
Outside FastAPI dependency injection, use `infra.require(PAYMENT_SERVICE)` when a
service is mandatory and `infra.get(PAYMENT_SERVICE)` when it is optional.
`require()` raises a consistent missing-service error and still validates typed
`ServiceKey` values.

Plugins can implement `PluginManifestHintsHook` by exposing `manifest_hints`.
The plugin manifest exposes release notes, migrations, production examples,
service references, scaffold additions, and default service key imports for
automation:

```json
{
  "payment": {
    "configured_services": ["payment"],
    "provides": ["payment"],
    "service_references": {
      "store_service": {
        "default_service": "database",
        "required_when": "default_provider != 'mock' in production",
        "required_when_config": {},
        "required_unless_config": {"default_provider": "mock"}
      }
    },
    "scaffold_files": [
      {
        "path": "app/search.py",
        "content": "def search_status() -> str:\n    return 'ready'\n"
      }
    ],
    "scaffold_readme_sections": [
      "## Search\n\nThis project has the search plugin enabled.\n"
    ],
    "service_keys": {"payment": "infra.plugins.PAYMENT_SERVICE"}
  }
}
```

`scaffold_files` writes extra relative POSIX paths when a project is created
with that plugin enabled. Paths must stay inside the generated project and
cannot overwrite core scaffold files. `scaffold_readme_sections` appends
plugin-owned usage notes to the generated project README. Built-in plugins still
own `app/main.py`, generated tests, worker wiring, and core release scripts;
external plugin packages should use scaffold files for plugin-specific modules,
scripts, examples, and docs.

`fastapi-infra config-check --settings infra.toml` validates plugin schemas,
manifest service-reference declarations, and manifest-declared service
references. Pass `--env-file .env` when the settings file uses
`{ "$env" = "NAME" }` references. For example, it reports when
`payment.store_service="database"` is configured without enabling the database
plugin, or when Redis-backed tasks/rate limiting reference an inactive
`database_service`.

## External Plugins

Applications can install third-party plugin packages that expose entry points in
the `fastapi_infra.plugins` group. `setup_infra()` always registers built-in
plugins, but it only imports external entry points that are explicitly named in
`InfraSettings.infra.plugins`. Installing an external plugin package is not
enough to affect startup.

```toml
[project.entry-points."fastapi_infra.plugins"]
search = "my_service_search.infra:SearchPlugin"
```

Use `plugins init` when starting a new external plugin package:

```bash
fastapi-infra plugins init search services/search_plugin
cd services/search_plugin
pip install -e ".[dev]"
python -m pytest -q
fastapi-infra plugins check search --settings infra.example.toml --lifecycle
```

The generated package includes a strict config model, service registration,
manifest hints, release-check hook, `infra.example.toml`, and conformance tests.
It is intentionally small so the plugin author can replace the example service
with real business integration code without keeping compatibility scaffolding.

Use `--kind provider` when the package should extend a built-in provider
registry instead of registering a new infra service. Generated provider
templates currently cover AI, payment, speech, storage, notifications, webhook,
task queue, and rate-limit adapters:

```bash
fastapi-infra plugins init openrouter providers/openrouter --kind provider --provider-kind ai
fastapi-infra plugins init adyen providers/adyen --kind provider --provider-kind payment
fastapi-infra plugins init deepgram providers/deepgram --kind provider --provider-kind speech
fastapi-infra plugins init r2 providers/r2 --kind provider --provider-kind storage
fastapi-infra plugins init twilio providers/twilio --kind provider --provider-kind notifications
fastapi-infra plugins init github providers/github --kind provider --provider-kind webhook
fastapi-infra plugins init nats providers/nats --kind provider --provider-kind tasks
fastapi-infra plugins init upstash providers/upstash --kind provider --provider-kind ratelimit
cd providers/openrouter
pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

The provider template exposes the matching provider entry point group such as
`fastapi_infra.ai_providers`, `fastapi_infra.payment_providers`, or
`fastapi_infra.speech_providers`, `fastapi_infra.storage_providers`, or
`fastapi_infra.notification_providers`, or `fastapi_infra.webhook_providers`,
`fastapi_infra.task_queue_backends`, or `fastapi_infra.ratelimit_backends`, plus
`fastapi_infra.provider_checks`. It includes a strict provider config model,
contract methods, health-check behavior where the owning plugin supports it,
certification metadata, and contract tests. Replace the template methods with
real SDK calls before publishing the package. Applications consume the provider
by enabling the built-in `ai`, `payment`, `speech`, `storage`, `notifications`,
`webhooks`, `tasks`, or `ratelimit` plugin and setting the provider config; no
compatibility shim or custom application wiring is required.

Template changes should pass the full template smoke script. It generates the
service plugin template and every provider/backend template, installs each
package editable into a private smoke work-dir target, runs package tests, then
runs `plugins check` or `config-check` through real entry point discovery:

```bash
python scripts/smoke_plugin_templates.py --work-dir /tmp/fastapi-infra-plugin-template-smoke
```

The entry point may load a plugin instance, a plugin class, or a no-argument
factory returning one plugin. The entry point name must match
`PluginMetadata.name`. The plugin still uses the same `InfraPlugin` contract,
metadata, strict config model, dependency ordering, health checks, and feature
flags as built-in plugins:

```python
class SearchPlugin:
    metadata = PluginMetadata(
        name="search",
        version="1.0.0",
        dependencies=["database"],
        provides=["search"],
    )
    config_model = SearchConfig

    def register(self, ctx: PluginContext) -> None:
        ctx.services["search"] = SearchService(ctx.services["database"], ctx.config)
```

For tests or tightly controlled applications, pass `plugins=[...]` to
`setup_infra()` or `PluginManager` to use an explicit plugin set instead of
auto-discovery.

`fastapi-infra new` can also scaffold explicitly requested external plugins
when the plugin package is installed and exposes an entry point:

```bash
fastapi-infra new services/search-api --plugins search
```

The CLI loads only external plugin names listed in `--plugins`; built-in profile
plugins still come from the bundled registry. The generated project uses the
plugin's manifest hints for config examples, env templates, extras, README
sections, and plugin-owned scaffold files.

See `examples/search_plugin` for a complete installable external plugin package
that is also exercised by the generated-project smoke script.

Scaffold hosts can pass the same complete registry to `create_project()` when
they need external plugin manifest hints to participate in project generation:

```python
from infra.plugins import get_builtin_plugins
from infra.scaffold import create_project

create_project(
    "services/search-api",
    "search_api",
    enabled_plugins=("search",),
    plugin_registry=[*get_builtin_plugins(), SearchPlugin()],
)
```

Before publishing a plugin package, run the conformance checker against the
entry point name:

```bash
fastapi-infra plugins check search --json
fastapi-infra plugins check search --settings infra.toml --lifecycle
```

The static check validates metadata, required methods, config schema,
`validate_config()`, manifest hints, and service-key declarations. The lifecycle
check additionally starts the selected plugins through `PluginManager`, runs
health checks, and shuts them down. `--settings` should point at a minimal config
that enables the external plugin and any required dependencies.

Plugins can add production-only release gates by implementing the optional
`PluginReleaseCheckHook` contract. The hook runs only when the plugin is
enabled. It may return
`ReleaseCheckIssue` instances, or plain issue mappings so third-party packages
do not need to import the central release-check module:

```python
from infra.plugins.release_checks import PluginReleaseIssue, release_error


class SearchPlugin:
    metadata = PluginMetadata(name="search", version="1.0.0", provides=["search"])
    config_model = SearchConfig

    def release_check(
        self,
        settings: InfraSettings,
        config: SearchConfig,
    ) -> list[PluginReleaseIssue]:
        if config.backend == "memory":
            return [
                release_error(
                    "memory_backend",
                    "production search requires a durable backend",
                )
            ]
        return []
```

Issue mappings require `code` and `message`; `plugin` defaults to the current
plugin name, and `severity` defaults to `error`. `severity` may be `error` or
`warning`. `infra.plugins.release_checks` provides small helpers for plugin
hooks, including `release_error()`, `release_warning()`, and
`enabled_plugin_config()` for reading another enabled plugin's validated config.

If a plugin's production behavior depends on another plugin's config, implement
`PluginReleaseDependencyHook` instead of making the target plugin inspect the
source plugin. The source plugin declares the dependency, and the central
release checker verifies it:

```python
from infra.plugins.release_checks import PluginReleaseDependency, release_dependency


class SearchPlugin:
    metadata = PluginMetadata(name="search", version="1.0.0", provides=["search"])
    config_model = SearchConfig

    def release_dependencies(
        self,
        settings: InfraSettings,
        config: SearchConfig,
    ) -> list[PluginReleaseDependency]:
        if config.backend != "external":
            return []
        return [
            release_dependency(
                "webhooks",
                "external_search_webhook_required",
                "external search requires webhooks.providers.search",
                config_path="providers.search",
            ),
            release_dependency(
                "webhooks",
                "external_search_webhook_required_provider_required",
                "external search requires webhooks.required_providers to include search",
                config_path="required_providers",
                contains="search",
            ),
        ]
```

Dependency issues are reported against the source plugin because it is the
plugin that introduced the production requirement. `config_path` uses dotted
paths inside the target plugin's `config`; omit it to require only that the
target plugin is enabled. Use `contains=` for lists, sets, strings, or mapping
keys, and `equals=` for exact values.

Provider-backed plugins should also implement the optional
`PluginProviderPolicyHook` contract so the central release checker can enforce
common gates without knowing the plugin's config model. Use
`provider_release_policies()` to list configured providers, local-only
providers, and whether live health probing is enabled:

```python
from infra.plugins.release_checks import PluginProviderPolicy, provider_policy


class SearchPlugin:
    metadata = PluginMetadata(name="search", version="1.0.0", provides=["search"])
    config_model = SearchConfig

    def provider_release_policies(
        self,
        settings: InfraSettings,
        config: SearchConfig,
    ) -> list[PluginProviderPolicy]:
        return [
            provider_policy(
                "search",
                {config.default_provider, *config.providers},
                local_providers={"memory"},
                health_probe=config.health_probe,
            )
        ]
```

This enables `uncertified_provider` and `health_probe_required` checks for
third-party provider plugins. The provider kind/name pairs are matched against
the active provider certification catalog.

To require a live provider certification report, implement the optional
`PluginProviderCertificationHook` contract:

```python
from infra.plugins.release_checks import PluginProviderCertification, provider_certification


class SearchPlugin:
    metadata = PluginMetadata(name="search", version="1.0.0", provides=["search"])
    config_model = SearchConfig

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: SearchConfig,
    ) -> list[PluginProviderCertification]:
        return [
            provider_certification("search", provider_name)
            for provider_name in sorted({config.default_provider, *config.providers})
        ]
```

Local/mock providers can still be returned here; they are ignored unless the
active certification catalog contains a matching `ProviderCheck`.

Provider registries also support narrow entry point groups for third-party
provider adapters:

```toml
[project.entry-points."fastapi_infra.ai_providers"]
acme = "acme_ai.provider:create_provider"

[project.entry-points."fastapi_infra.payment_providers"]
adyen = "acme_payments.adyen:create_provider"

[project.entry-points."fastapi_infra.speech_providers"]
deepgram = "acme_speech.deepgram:create_provider"

[project.entry-points."fastapi_infra.storage_providers"]
r2 = "acme_storage.r2:create_provider"

[project.entry-points."fastapi_infra.notification_providers"]
twilio = "acme_notifications.twilio:create_provider"

[project.entry-points."fastapi_infra.task_queue_backends"]
sqs = "acme_tasks.sqs:create_queue"

[project.entry-points."fastapi_infra.ratelimit_backends"]
upstash = "acme_ratelimit.upstash:create_limiter"

[project.entry-points."fastapi_infra.webhook_providers"]
github = "acme_webhooks.github:create_provider"
```

Each provider entry point loads a factory with one argument: the provider config
mapping from `InfraSettings`. The returned provider's `name` must match the
entry point name. This keeps the extension contract small and avoids importing
unconfigured providers during startup.

Third-party provider packages can also expose production certification metadata
with `fastapi_infra.provider_checks`:

```toml
[project.entry-points."fastapi_infra.provider_checks"]
acme_ai = "acme_ai.certification:provider_checks"
```

The entry point should return `ProviderCheck` objects that bind a runtime
provider to live tests:

```python
from infra.provider_certification import ProviderCheck


def provider_checks() -> tuple[ProviderCheck, ...]:
    return (
        ProviderCheck(
            name="acme-ai",
            provider_kind="ai",
            provider_name="acme",
            tests=("test_live_acme_chat",),
            test_path="tests/integration/test_acme_live.py",
            required_env=("ACME_API_KEY",),
            required_packages=("acme-sdk",),
        ),
    )
```

`fastapi-infra release-check` accepts third-party providers only when the active
certification catalog contains a matching `provider_kind`/`provider_name` and
the certification report includes passing evidence for the declared check.

## AI

The AI plugin registers an `AIRegistry` with provider adapters for mock,
OpenAI, Anthropic, and Gemini. SDK-backed providers are configured under
`providers` and still lazy-load their optional SDKs only when called:

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "ai": {
                "enabled": True,
                "config": {
                    "default_provider": "openai",
                    "health_probe": True,
                    "providers": {
                        "openai": {
                            "api_key": "sk-...",
                            "base_url": "https://api.openai.com/v1",
                            "timeout": 10,
                        }
                    },
                },
            }
        }
    }
)
```

Health checks do not call AI vendors by default. Set `health_probe=True` in
production to call the provider's model-list endpoint through the configured SDK
client and verify credentials/upstream reachability.

Chat and embeddings share the same registry. OpenAI and Gemini implement
embeddings through their SDK APIs. Providers that do not support embeddings
raise `NotImplementedError` instead of returning placeholder vectors.

```python
from infra.plugins import AI_SERVICE
from infra.plugins.ai import EmbeddingRequest

ai = infra.require(AI_SERVICE)
response = await ai.embed(
    EmbeddingRequest(model="text-embedding-3-small", input=["hello", "world"]),
    provider="openai",
)
```

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
For the HTTP plugin, `base_url` may be empty or an absolute `http`/`https` URL,
and `timeout` must be positive. Default headers are treated as sensitive config
because they often carry `Authorization` tokens.
The lower-level `HttpClient` accepts an optional `HttpRetryConfig`. Retries are
off unless configured; when enabled, only idempotent methods are retried by
default, and only for temporary failures such as timeouts, connection errors,
`429`, and selected `5xx` responses. Enable `retry_all_methods=True` only for
calls that are safe to replay.
When both `observability` and `http` are enabled, the HTTP plugin passes the
observability service into `HttpClient`. Outbound calls then record
`http_client_requests_total`, `http_client_attempts_total`,
`http_client_responses_total`, `http_client_errors_total`,
`http_client_retries_total`, and `http_client_request_seconds`, and they
propagate the current `X-Trace-ID` and `X-Request-ID` unless the caller already
provided those headers.
The cache plugin uses the configured `database_service` when it exists. If no
database service is active, it creates its own `DatabaseManager` from
`database_config` and closes only that owned manager during plugin shutdown.
Lower-level database consumers such as repositories, unit-of-work, distributed
locks, and streams require an explicit `DatabaseManager` argument; they do not
create unmanaged default connections.
`DistributedLockManager` is a Redis lease lock with token-checked release and
extension. It prevents accidental unlock by another holder, but it is not a
fencing-token implementation; workflows that need strict write ordering should
also use storage-level version checks.
The database plugin health check calls the registered database service's real
`health_check()` method; failed checks report `unhealthy` instead of returning a
hard-coded healthy status.

## Database Migrations

The package includes a lightweight SQL migration runner for projects that do
not need a full Alembic setup on day one:

```bash
fastapi-infra migrations new migrations create_users
fastapi-infra migrations list migrations
fastapi-infra migrations migrate migrations --settings infra.toml
```

Migration filenames must use `YYYYMMDDHHMMSS_name.sql`. Applied migrations are
stored in `infra_schema_migrations` with a checksum; changing an applied file is
rejected before new migrations run.

```python
from infra.database import SqlMigrationRunner
from infra.plugins import DATABASE_SERVICE

database = infra.require(DATABASE_SERVICE)
applied = await SqlMigrationRunner(database, "migrations").migrate()
```

Production runners can pass an explicit `lock` and `transaction_factory`.
`migrate()` acquires the lock for the full run, releases it in `finally`, and
executes each migration file plus its version insert through the transaction
executor when one is provided. The runner does not auto-detect driver-specific
transaction or lock APIs; projects wire those in deliberately.

The CLI `migrate` command loads `InfraSettings`, starts the configured database
plugin, applies pending SQL files, and shuts the plugin manager down.

Plugins can publish schema migrations in their manifest. The scaffold writes
those migrations into the generated project's `migrations/` directory using
stable `version_name.sql` filenames. `payment` publishes
`00000000001000_infra_payment_store.sql`; `webhooks` publishes
`00000000001100_infra_webhook_store.sql`. Run release checks with
`--migrations migrations` to verify enabled plugin migrations are present:

```bash
fastapi-infra release-check --settings infra.production.example.toml \
  --env-file .env \
  --migrations migrations \
  --static-only
```

`DatabaseManager` keeps MySQL and Redis opt-in independently. Use
`{"mysql_enabled": True, "redis_enabled": False}` for database-only services and
`{"mysql_enabled": False, "redis_enabled": True}` for cache/queue-only services.

## Project Scaffold

`fastapi-infra new path/to/service --profile api --plugins tasks` creates a
small project from a named plugin profile plus any extra requested plugins. It
includes `app/main.py`, `app/settings.py`, `tests/test_config.py`,
`tests/test_health.py`, `Dockerfile`, `Makefile`, `compose.yaml`, `.dockerignore`, `.gitignore`,
`.github/workflows/ci.yml`, `AGENTS.md`, `README.md`, `.env.example`, `provider.env.example`, `infra.toml`,
`infra.production.example.toml`, `scripts/prepare-env.sh`,
`scripts/verify-release.sh`, and `infra.manifest.json`.
Enabled plugins get local config examples
from the plugin manifest, so the generated `infra.toml` can pass
`fastapi-infra config-check --settings infra.toml` before you add production
credentials. The generated `pyproject.toml` includes a `dev` extra for pytest
and FastAPI `TestClient` support, so a clean project can run its generated tests
with `pip install -e ".[dev]"`. The production example uses manifest
`production_config_example` values and renders provider secrets as
`{ "$env" = "NAME" }` references. The generated config test validates
`infra.toml` through both the Python validation API and the
`fastapi-infra config-check --settings infra.toml` CLI path; the generated
health test also verifies trace/request response headers, security headers, and
enabled plugin services, not only that `/health` returns a response.
`infra.manifest.json` is the machine-readable project contract for CI and AI
agents: it records the scaffold profile, explicitly requested plugins, enabled
plugins, production plugins, package plugins, key files, standard verification
commands, and a compact plugin service/env summary. Run
`fastapi-infra project-check .` to verify that the generated files, Makefile,
release script, CI workflow, Dockerfile, `infra.toml`, and
`infra.production.example.toml` still match that contract. It also checks that
`AGENTS.md` tells agents to use the manifest, Makefile gates, and separated
runtime/provider env files, and that
the manifest `commands` still point at the standard `make env`, `make verify`,
`make release-static`, `make provider-preflight`, and `make dev-up` entries, and
that `package_plugins` plus the per-plugin summary entries match the enabled and
production plugin sets. It also verifies that `pyproject.toml` depends on
`fastapi-infra[...]` with the recommended extras required by those package
plugins. The generated CI workflow calls `make env`, `make verify`, and
`make release-static`, so local, CI, and smoke-test validation share the same
command surface. The generated `scripts/verify-release.sh` reuses the same
Makefile gates before provider preflight and optional live certification, and
the generated-project smoke script installs external plugins into a private
work-dir target, fills `provider.env` blanks with CI placeholders, and then runs
that release script directly. The
`.dockerignore` contract keeps `.env`, `provider.env`,
`provider-env-template.env`, `provider-certification.json`, and
`provider-preflight.json` out of Docker build
contexts, and `.gitignore` keeps the same local secrets and evidence artifacts
out of commits. The generated
Makefile exposes `make env`, `make verify`, `make release-static`,
`make provider-preflight`, `make release`, and `make dev-up` so local developers and AI agents use the same
stable command surface.
`make env` runs `scripts/prepare-env.sh`, which creates `.env` and
`provider.env` and replaces the unsafe example `JWT_SECRET` with a generated
local secret for auth profiles.
The generated
Dockerfile copies `infra.manifest.json` and `scripts/`, runs as a non-root
`appuser`, exposes a `/health` container health check, and copies `migrations/`
when the production plugin set includes `database`.
`compose.yaml` builds the app with `INFRA_SETTINGS=infra.production.example.toml`
and wires MySQL or Redis services when the production config requires them.

Generated code remains tied only to enabled plugins:

- `app/main.py` always installs `ErrorHandlingMiddleware`,
  `RequestLoggingMiddleware`, and `SecurityHeadersMiddleware`, so new services
  start with the shared error shape, trace/request id propagation, and baseline
  security headers.
- Without `observability`, `app/main.py` does not import observability helpers.
- With `observability`, `app/main.py` installs request metrics middleware and
  `/ops/healthz`, `/ops/readyz`, and `/ops/metrics`.
- With `tasks`, the scaffold includes `app/worker.py` using `TaskWorker` and
  the generated infra context, with an `example.ping` handler as the registration
  point.
- With `database`, the scaffold includes `migrations/.gitkeep` and README
  migration commands, including `fastapi-infra migrations migrate`.
- With plugins that publish schema migrations, the scaffold writes those SQL
  files into `migrations/` even when the database plugin is only part of the
  production profile.

## Auth

The auth plugin provides hashed API-key authentication plus stdlib HS256 JWT
issuing and verification. API keys are configured only through
`hashed_api_keys`, which uses PBKDF2-HMAC-SHA256 encoded as
`algorithm$iterations$salt$hash`. There is no plaintext `api_keys` config path.

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "auth": {
                "enabled": True,
                "config": {
                    "hashed_api_keys": {
                        "primary": {
                            "key_hash": "pbkdf2_sha256$260000$...",
                            "subject": "user-1",
                            "scopes": ["read:items"],
                            "roles": ["admin"],
                        }
                    },
                    "jwt_secret": "production-jwt-secret-at-least-32-chars",
                    "jwt_signing_keys": {
                        "previous": {"secret": "previous-jwt-secret-at-least-32-chars"},
                        "current": {"secret": "current-jwt-secret-at-least-32-chars"},
                    },
                    "jwt_key_id": "current",
                    "jwt_issuer": "fastapi-infra",
                    "jwt_audience": "api",
                    "access_token_ttl_seconds": 3600,
                },
            }
        }
    }
)
```

Generate stored hashes with `infra.plugins.auth.hash_api_key("real-api-key")`.
For key rotation, add a new `hashed_api_keys` record with its own key id, deploy
both hashes during migration, then remove the old record.

If the auth plugin is enabled without `hashed_api_keys` and without
`jwt_secret` or `jwt_signing_keys`, its health status is `degraded`. This keeps
generated projects bootable while making it visible that authentication has no
usable credentials yet.

`jwt_secret` is a single-key shortcut. Production services can configure
`jwt_signing_keys` plus `jwt_key_id`; issued JWTs include `kid`, and verification
accepts any configured key so services can deploy a new signing key before
removing the old one. Issuer and audience are optional, but when configured they
are required on incoming JWTs.
`fastapi-infra release-check` rejects short JWT secrets, common placeholder
values, and API key hashes that do not use the current PBKDF2-HMAC-SHA256
encoding and iteration floor.

FastAPI routes can use dependency helpers directly. They read the auth service
from `request.app.state.infra`, accept `Authorization: Bearer <jwt>` and
`X-API-Key`, and raise `401` for authentication failures or `403` for missing
scopes/roles:

```python
from typing import Annotated

from fastapi import Depends
from infra.plugins.auth import Principal, require_roles, require_scopes


@app.get("/admin/items")
async def admin_items(
    principal: Annotated[
        Principal,
        Depends(require_scopes("items:read")),
    ],
):
    return {"subject": principal.subject}
```

## Payment

The payment plugin registers a `PaymentService` backed by a provider registry.
The built-in `mock` provider is deterministic and safe for local development.

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "payment": {
                "enabled": True,
                "config": {
                    "default_provider": "mock",
                    "providers": {"mock": {}},
                },
            }
        }
    }
)
```

Business code calls the service, not a provider directly:

```python
from infra.plugins import PAYMENT_SERVICE

payment = infra.require(PAYMENT_SERVICE)
checkout = await payment.create_checkout(
    amount=1250,
    currency="usd",
    reference="order-123",
)
status = await payment.get_payment_status(checkout.id)
refund = await payment.create_refund(
    checkout_id=checkout.id,
    amount=1250,
    currency="usd",
)
```

Stripe is available as a real provider without adding the Stripe SDK. It uses
Stripe Checkout Sessions over HTTP and validates webhook signatures with
Stripe's `t=...,v1=...` HMAC SHA256 format. Missing `api_key` or unknown provider
names fail during plugin startup.

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "payment": {
                "enabled": True,
                "config": {
                    "default_provider": "stripe",
                    "health_probe": True,
                    "providers": {
                        "stripe": {
                            "api_key": "sk_live_...",
                            "webhook_secret": "whsec_...",
                            "timeout": 30.0,
                            "max_attempts": 3,
                            "retry_base_delay": 0.25,
                        }
                    },
                },
            }
        }
    }
)
```

`timeout` is the per-request Stripe API timeout in seconds. It defaults to
`30.0`. The Stripe provider retries `409`, `429`, `5xx`, and transport errors
with exponential backoff. `max_attempts` defaults to `3`, and
`retry_base_delay` defaults to `0.25` seconds. Non-retryable `4xx` API errors
raise `StripeAPIError` without another request. Custom `api_base` values must
be absolute `http` or `https` URLs.
Health checks do not call Stripe by default. Set `health_probe=True` in
production to probe `GET /v1/account` with the configured API key.
Checkout and refund writes derive a stable Stripe `Idempotency-Key` from
`reference` when `provider_options["idempotency_key"]` is not supplied. Pass an
explicit idempotency key for workflows that need a different deduplication
boundary.

```python
checkout = await payment.create_checkout(
    amount=1250,
    currency="usd",
    reference="order-123",
    success_url="https://example.com/success",
    cancel_url="https://example.com/cancel",
)
```

Stripe refunds use the real `POST /v1/refunds` API. Pass
`provider_options={"payment_intent": "...", "idempotency_key": "..."}` or
`{"charge": "..."}`. If neither is supplied, the provider retrieves the Checkout
Session and uses its `payment_intent` when present.

`PaymentService` can be given a store to persist provider results. The store is
intentionally narrow: it records checkout and refund ids, statuses, amounts, and
provider names; business order ownership stays in the application. When the
database plugin is enabled first, configure `store_service` to have the payment
plugin attach `SqlPaymentStore` automatically. Production release checks require
that database-backed payment storage has MySQL enabled; the live provider
workflow certifies MySQL together with Stripe because Stripe payment results are
not considered durable without the store backend:

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "database": {"enabled": True},
            "payment": {
                "enabled": True,
                "config": {"default_provider": "stripe", "store_service": "database"},
            },
        }
    }
)
```

`SqlPaymentStore` creates `infra_payment_checkouts` and
`infra_payment_refunds` and upserts provider snapshots by `(provider, id)`.
The payment plugin manifest publishes the same default schema as
`00000000001000_infra_payment_store.sql` for scaffolded projects.

## Webhooks

The webhooks plugin includes an inbound FastAPI route installer. It parses the
raw request body through a registered provider, deduplicates by
`(provider, event id)`, and dispatches only new events. The default in-memory
store is for local development. Production deployments should enable
`durable_store`, configure signed providers under `providers`, and pass a
persistent store such as `SqlWebhookStore` when installing routes. The dispatcher
carries those runtime requirements, so route installation fails if required
providers are missing:

```python
from infra.plugins.webhooks import (
    SqlWebhookStore,
    install_webhook_routes,
)
from infra.plugins import DATABASE_SERVICE, WEBHOOKS_SERVICE

database = infra.require(DATABASE_SERVICE)
dispatcher = infra.require(WEBHOOKS_SERVICE)
install_webhook_routes(
    app,
    dispatcher,
    store=SqlWebhookStore(database),
)
```

The default route is `POST /webhooks/{provider}`. Provider handling is
provider-aware: a request to `/webhooks/stripe` uses the registered `stripe`
provider to verify the raw payload and extract the event id/type. Production
configs should set `required_providers` to providers that must be installed at
route setup. Invalid signatures return `401`, unknown providers return `404`,
invalid JSON returns `400`, and duplicate events return
`{"status": "duplicate"}` without dispatching again.
Stripe payment declares this as a release dependency: if `payment` uses the
`stripe` provider, release checks require `webhooks.providers.stripe` and
`webhooks.required_providers` to include `stripe`. The Webhooks plugin does not
inspect Payment config directly.
`SqlWebhookStore` creates an `infra_webhook_events` table with
`(provider, event_id)` as the primary key, so duplicate webhook deliveries remain
deduplicated across process restarts and horizontally scaled workers. The
webhooks plugin manifest publishes the default schema as
`00000000001100_infra_webhook_store.sql`.

## Rate Limiting

The rate-limit plugin exposes one service with `allow(key, limit, window_seconds)`.
The memory provider is for local development. Production services should use the
Redis provider and enable the database plugin with `redis_enabled=true`:

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "database": {
                "enabled": True,
                "config": {"config": {"mysql_enabled": False, "redis_enabled": True}},
            },
            "ratelimit": {
                "enabled": True,
                "config": {
                    "default_provider": "redis",
                    "providers": {
                        "redis": {
                            "database_service": "database",
                            "key_prefix": "myapp:ratelimit",
                        }
                    },
                },
            },
        }
    }
)

from infra.plugins import RATELIMIT_SERVICE

limiter = infra.require(RATELIMIT_SERVICE)
allowed = await limiter.allow("client:123", limit=100, window_seconds=60)
```

Third-party rate limiters can be exposed through
`fastapi_infra.ratelimit_backends`; the returned provider must implement
`allow(key, limit, window_seconds)`.

FastAPI routes can use the bundled dependency helper:

```python
from fastapi import Depends

from infra.plugins.ratelimit import rate_limit


@app.get("/search", dependencies=[Depends(rate_limit(limit=60, window_seconds=60))])
async def search() -> dict[str, bool]:
    return {"ok": True}
```

The default key is the connecting client IP. Pass `key_func` when the quota should
be scoped to an account, tenant, or API key instead. Blocked requests return
`429` with `Retry-After`, `X-RateLimit-Limit`, and `X-RateLimit-Window` headers.

The Redis provider uses atomic `INCR` plus `EXPIRE` over fixed windows. Release
checks reject the memory provider and require a Redis backing for production.

## Storage

The storage plugin exposes a single object-store style API:

```python
from infra.plugins import STORAGE_SERVICE

storage = infra.require(STORAGE_SERVICE)
await storage.put_object("reports/monthly.json", b"{}", content_type="application/json")
body = await storage.get_object("reports/monthly.json")
exists = await storage.exists("reports/monthly.json")
await storage.delete_object("reports/monthly.json")
```

Local storage is the default provider and stores files under a configured root.

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "storage": {
                "enabled": True,
                "config": {"root": "/tmp/myapp-storage"},
            }
        }
    }
)
```

S3-compatible storage is available through a stdlib HTTP implementation with AWS
Signature V4 signing. It does not import `boto3`.

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "storage": {
                "enabled": True,
                "config": {
                    "default_provider": "s3",
                    "health_probe": True,
                    "providers": {
                        "s3": {
                            "bucket": "assets",
                            "region": "us-east-1",
                            "access_key_id": "...",
                            "secret_access_key": "...",
                            "endpoint_url": "https://s3.example.com",
                            "force_path_style": True,
                            "timeout": 30.0,
                            "max_attempts": 3,
                            "retry_base_delay": 0.25,
                        }
                    },
                },
            }
        }
    }
)
```

`timeout` is the per-request stdlib HTTP timeout in seconds. External storage
health remains `degraded` by default because startup health checks avoid
surprise network calls. The S3 provider retries `409`, `429`, `5xx`, and
transport errors with exponential backoff. `max_attempts` defaults to `3`, and
`retry_base_delay` defaults to `0.25` seconds. Set `health_probe=True` in
production to run a signed `HEAD` probe against the configured bucket.

## Notifications

The notifications plugin defaults to `noop` for local development and tests. It
records skipped deliveries and never claims a message was sent.

Like storage, payment, speech, and AI, notifications use a provider registry.
Built-in providers are `noop`, `smtp`, and `webhook`; third-party packages can
register adapters through `fastapi_infra.notification_providers`.

SMTP is available as a real provider using Python's stdlib `smtplib`. Missing
`host` or `sender` fails during plugin startup.

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "notifications": {
                "enabled": True,
                "config": {
                    "default_provider": "smtp",
                    "health_probe": True,
                    "providers": {
                        "smtp": {
                            "host": "smtp.example.com",
                            "port": 587,
                            "sender": "noreply@example.com",
                            "username": "mailer",
                            "password": "...",
                            "use_tls": True,
                            "timeout": 30.0,
                            "max_attempts": 3,
                            "retry_base_delay": 0.25,
                        }
                    },
                },
            }
        }
    }
)
```

SMTP health reports `degraded` by default because startup only validates
configuration. Set `health_probe=True` in production to connect, start TLS, and
login without sending an email. The SMTP provider retries temporary connection
errors and temporary SMTP response codes with exponential backoff. `max_attempts`
defaults to `3`, and `retry_base_delay` defaults to `0.25` seconds. `port`,
`timeout`, and retry options are validated before startup reaches SMTP.
Authentication failures and sender/recipient rejection are not retried. Use live
provider certification before claiming SMTP production readiness.

Generic outbound webhook notifications are also available when a service needs
to notify another HTTP system without a provider-specific adapter:

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "notifications": {
                "enabled": True,
                "config": {
                    "default_provider": "webhook",
                    "health_probe": True,
                    "providers": {
                        "webhook": {
                            "url": "https://hooks.example.com/notify",
                            "health_url": "https://hooks.example.com/health",
                            "signing_secret": "...",
                            "timeout": 10.0,
                        }
                    },
                },
            }
        }
    }
)
```

The webhook provider sends notification payloads as JSON. When `signing_secret`
is configured it adds `x-infra-timestamp` and `x-infra-signature` HMAC-SHA256
headers. Release checks require `signing_secret`, `health_url`, and
`health_probe=True` for production webhook notifications.

```python
from infra.plugins import NOTIFICATIONS_SERVICE

notifications = infra.require(NOTIFICATIONS_SERVICE)
result = await notifications.send(
    channel="email",
    recipient="user@example.com",
    subject="Subject",
    body="Body",
)
assert result.status == "sent"
```

## Speech

The speech plugin registers a `SpeechService` for ASR and TTS. It follows the
same provider-registry shape as payment and AI. The built-in `mock` provider is
safe for local development and tests.

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "speech": {
                "enabled": True,
                "config": {
                    "default_provider": "mock",
                    "providers": {"mock": {}},
                },
            }
        }
    }
)
```

```python
from infra.plugins import SPEECH_SERVICE

speech = infra.require(SPEECH_SERVICE)
transcription = await speech.transcribe(b"audio", format="wav")
synthesis = await speech.synthesize("hello", voice="default")
```

External ASR/TTS vendors should be added as providers behind this service
interface, not called directly from application routes.

OpenAI is available as a real provider using the Audio API over stdlib HTTP.
It sends ASR requests to `/v1/audio/transcriptions` as `multipart/form-data`
and TTS requests to `/v1/audio/speech` as JSON. Missing `api_key` fails during
plugin startup.

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "speech": {
                "enabled": True,
                "config": {
                    "default_provider": "openai",
                    "health_probe": True,
                    "providers": {
                        "openai": {
                            "api_key": "sk-...",
                            "asr_model": "gpt-4o-mini-transcribe",
                            "tts_model": "gpt-4o-mini-tts",
                            "voice": "alloy",
                            "asr_response_format": "json",
                            "tts_response_format": "mp3",
                            "timeout": 60.0,
                            "max_attempts": 3,
                            "retry_base_delay": 0.25,
                        }
                    },
                },
            }
        }
    }
)
```

`timeout` is the per-request OpenAI speech HTTP timeout in seconds. It defaults
to `60.0`. The OpenAI Speech provider retries `408`, `409`, `429`, `5xx`, and
transport errors with exponential backoff. `max_attempts` defaults to `3`, and
`retry_base_delay` defaults to `0.25` seconds. Custom `api_base` values must be
absolute `http` or `https` URLs.
Health checks do not call OpenAI by default. Set `health_probe=True` in
production to retrieve the configured ASR/TTS models from `/v1/models/...`.

## Live Provider Tests

The default unit tests validate adapter boundaries with fake transports and mock
providers. They do not require real credentials and should not depend on live
network access.

Opt-in live tests live under `tests/integration/`. They are collected by pytest,
but each test skips unless its provider-specific environment variables are set:

- MySQL round trip: `MYSQL_LIVE_HOST`, `MYSQL_LIVE_USER`,
  `MYSQL_LIVE_PASSWORD`, `MYSQL_LIVE_DB`, optional `MYSQL_LIVE_PORT`,
  `MYSQL_LIVE_CONNECT_TIMEOUT`.
- Redis cache round trip: `REDIS_LIVE_URL`, optional
  `REDIS_LIVE_CONNECT_TIMEOUT`.
- Stripe checkout: `STRIPE_API_KEY`, optional `STRIPE_API_BASE`,
  `STRIPE_LIVE_TIMEOUT`.
- Stripe webhook signature: `STRIPE_WEBHOOK_SECRET`.
- S3 put/get/list/presign: `S3_LIVE_BUCKET`, `S3_LIVE_REGION`,
  `S3_LIVE_ACCESS_KEY_ID`, `S3_LIVE_SECRET_ACCESS_KEY`, optional
  `S3_LIVE_ENDPOINT_URL`, `S3_LIVE_FORCE_PATH_STYLE`, `S3_LIVE_PREFIX`,
  `S3_LIVE_TIMEOUT`.
- OpenAI chat/embeddings: `OPENAI_API_KEY`, `OPENAI_LIVE_CHAT_MODEL`,
  `OPENAI_LIVE_EMBEDDING_MODEL`, optional `OPENAI_API_BASE`,
  `OPENAI_LIVE_TIMEOUT`.
- Anthropic chat: `ANTHROPIC_API_KEY`, `ANTHROPIC_LIVE_CHAT_MODEL`,
  optional `ANTHROPIC_API_BASE`, `ANTHROPIC_LIVE_TIMEOUT`.
- Gemini chat/embeddings: `GEMINI_API_KEY`, `GEMINI_LIVE_CHAT_MODEL`,
  `GEMINI_LIVE_EMBEDDING_MODEL`, optional `GEMINI_API_BASE`,
  `GEMINI_LIVE_TIMEOUT`.
- OpenAI speech ASR/TTS: `OPENAI_API_KEY`, optional `OPENAI_API_BASE`,
  `OPENAI_ASR_MODEL`, `OPENAI_TTS_MODEL`, `OPENAI_VOICE`,
  `OPENAI_SPEECH_TIMEOUT`.
- SMTP email send: `SMTP_LIVE_HOST`, `SMTP_LIVE_SENDER`,
  `SMTP_LIVE_RECIPIENT`, optional `SMTP_LIVE_PORT`, `SMTP_LIVE_USERNAME`,
  `SMTP_LIVE_PASSWORD`, `SMTP_LIVE_USE_TLS`, `SMTP_LIVE_TIMEOUT`.

Run them explicitly with:

```bash
pip install -e ".[dev,live-providers]"
fastapi-infra release-check --settings infra.toml
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --env-template > provider-env-template.env
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --env-file provider.env --preflight --json > provider-preflight.json
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --env-file provider.env --json > provider-certification.json
fastapi-infra release-check --settings infra.toml \
  --provider-certification-report provider-certification.json
```

Use `fastapi-infra release-check` before live tests to reject production configs
that still use mock/local/noop/memory providers or leave external provider
health probes disabled. It also blocks cache without Redis, payment database
stores without MySQL, and webhook configs that do not declare durable storage
and signature verification. The memory rate-limit provider is local-only and is
blocked by release-check. Observability with memory metrics or disabled tracing
is reported as a warning so teams can decide whether a given service requires
Prometheus and OpenTelemetry. Use `fastapi-infra certify-providers` for the
external provider release gate. It treats skipped live tests as not certified
and can emit JSON evidence for CI artifacts. Use `--env-file provider.env` to
load dotenv-style live credentials for both preflight and live tests without
requiring them to be exported in the shell. Pass
`--settings infra.production.example.toml` to select only provider checks required
by the active production config; dependencies are included automatically, so a
Stripe payment config also selects the MySQL provider check. Use
`--settings-env-file .env` for runtime config secrets referenced by the settings
file, and `--env-file provider.env` for live provider certification credentials.
Pass the certification JSON back
into `release-check --provider-certification-report` when the release gate must
prove both static config safety and live provider certification.
`release-check` requires live certification evidence by default for any configured
external provider; use `--static-only` only for a local static scan. Release-check
expects the certification report to cover every known real provider declared in
the configuration, not only the plugin's `default_provider`. For configured real
providers, the report must include a parseable `generated_at` timestamp and is
treated as stale after 24 hours. Release-check also validates provider result
test names, test paths, and requirement metadata against the current
certification catalog, including third-party `fastapi_infra.provider_checks`
entry points, so old reports cannot skip newly required live checks. Summary
counts must match the provider result entries exactly; duplicate or malformed
provider results are rejected. `selected_providers` must be unique and must
match the provider result names exactly. Production release checks require
reports to cover every selected provider check's declared `test_path`.

## Tasks

The tasks plugin defaults to the in-memory queue. Use the Redis Streams provider
when you need cross-process task handoff:

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "database": {"enabled": True},
            "tasks": {
                "enabled": True,
                "config": {
                    "default_provider": "redis",
                    "providers": {
                        "redis": {
                            "database_service": "database",
                            "stream_name": "myapp:tasks",
                            "consumer_group": "workers",
                            "consumer_name": "worker-1",
                            "pending_min_idle_ms": 60000,
                        }
                    },
                },
            },
        }
    }
)
```

The Redis provider creates the consumer group if needed, performs non-blocking
dequeue calls, and attempts stale pending recovery before reading new messages.
`get()` reads the local cache; use `await get_async(task_id)` to load persisted
state into a new queue instance. Tasks health checks validate that the registered
queue exists; Redis-backed queues call `PING`, so broken Redis connectivity marks
the plugin unhealthy instead of reporting fabricated success.
Production release checks require a verifiable Redis backing for the Redis
provider. File-based task config only describes provider and stream parameters;
Redis clients are runtime objects and can be injected with `TasksPlugin(redis=client)`
for tests or embedded runtimes. Production release checks cannot prove that runtime
injection from a config file, so production Redis task config should enable the
database plugin and keep `redis_enabled=true`. The provider certification report
must also cover `redis` whenever the Redis task provider is enabled.
Third-party task queues can be exposed through
`fastapi_infra.task_queue_backends`; the returned provider must implement the
`TaskQueue` protocol.

Use `TaskWorker` when you need an executable worker runtime:

```python
from infra.plugins.tasks import (
    TaskEnvelope,
    TaskWorker,
    TaskWorkerRunConfig,
    run_task_worker,
)

from infra.plugins import OBSERVABILITY_SERVICE, TASKS_SERVICE

queue = infra.require(TASKS_SERVICE)
task = await queue.enqueue(
    "index_document",
    {"id": "doc-1"},
    idempotency_key="index:doc-1",
    delay_seconds=30,
    max_attempts=3,
)
observability = infra.get(OBSERVABILITY_SERVICE)
worker = TaskWorker(queue, retry_backoff=2, instrumentation=observability)


@worker.handler("index_document")
async def index_document(task: TaskEnvelope) -> None:
    ...


stats = await run_task_worker(
    worker,
    TaskWorkerRunConfig(idle_sleep=0.5, require_handlers=True, concurrency=4),
)
```

`run_task_worker()` installs SIGINT/SIGTERM handlers for dedicated worker
processes and returns `TaskWorkerRunStats` with `processed`, `idle_polls`, and
`stopped`, plus outcome counters for `completed`, `retried`, and `dead_lettered`.
`TaskWorker.run()` also supports `max_tasks`, `idle_poll_limit`, and
`concurrency` for bounded batch workers, tests, and concurrent worker processes.
`run_once()` returns whether a task was
processed. `enqueue()` accepts `idempotency_key` and `delay_seconds` for
deduplicating business submissions and delaying first delivery; a repeated
idempotency key returns the original task instead of publishing another message.
Each delivery increments `attempts`; handler exceptions call `queue.retry()`
until `max_attempts` is exhausted, then `queue.dead_letter()`.
Missing handlers go straight to dead-letter because retrying them would hide a
deployment or routing error. Pass `instrumentation=infra.get(OBSERVABILITY_SERVICE)`
when observability is enabled to record `task_worker_tasks_total`,
`task_worker_completed_total`, `task_worker_retried_total`,
`task_worker_dead_lettered_total`, `task_worker_idle_polls_total`, and
`task_worker_task_seconds`, plus a `task.worker.run` span for each processed
task.

## Observability Routes

The observability plugin registers the in-memory observability service, but HTTP
routes are opt-in. Install them explicitly on the FastAPI app:

```python
from infra.plugins.observability import install_observability_routes

install_observability_routes(app, infra, prefix="/ops")
```

With `prefix="/ops"`, the helper adds:

- `GET /ops/healthz`: returns the cached health snapshot.
- `GET /ops/readyz`: refreshes active plugin health checks and returns the
  refreshed snapshot under `statuses`. Refreshes run concurrently with a
  five-second timeout per plugin; pass `readiness_timeout_seconds` to tune that
  budget. It returns `503` only when at least one status is `unhealthy`;
  degraded, disabled, and healthy statuses return `200`.
- `GET /ops/metrics`: returns Prometheus text exposition lines such as
  `# TYPE requests_total counter`, `requests_total 3`,
  `request_seconds_count 2`, and `request_seconds_sum 0.5`.

Metric output intentionally stays lightweight by default, but it uses the
`text/plain; version=0.0.4` content type and stable `# TYPE` metadata. Metric
names are normalized to scrape-safe identifier characters before they are
written. The route helper raises `RuntimeError` if any target route already
exists, including when the helper is installed more than once for the same
prefix.

For production Prometheus integration, install `fastapi-infra[observability]`
and select the standard client backend:

```python
from infra import InfraSettings

settings = InfraSettings(
    infra={
        "plugins": {
            "observability": {
                "enabled": True,
                "config": {
                    "metrics_backend": "prometheus",
                    "tracing_backend": "opentelemetry",
                },
            }
        }
    }
)
```

The `prometheus` backend records the same request counters and timers through a
dedicated `prometheus_client.CollectorRegistry`. If `prometheus-client` is not
installed, startup fails with an explicit dependency error instead of silently
falling back to in-memory metrics.

The `opentelemetry` tracing backend wraps middleware requests in spans from the
process-global OpenTelemetry tracer provider. Exporters stay application-owned,
so infra does not force Jaeger, OTLP, or any vendor-specific transport.

Request metrics are opt-in through middleware:

```python
from infra.plugins.observability import install_observability_middleware

install_observability_middleware(app)
```

The middleware records request count, status-code counters, duration timers, and
exception counters against the real `ObservabilityService`.

Request tracing and error formatting are separate from metrics:

```python
from infra.middleware import (
    CORSMiddleware,
    ErrorHandlingMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
    install_error_handlers,
)

install_error_handlers(app)
app.add_middleware(ErrorHandlingMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["https://app.example.com"])
```

`RequestLoggingMiddleware` accepts upstream `X-Trace-ID`, `X-Request-ID`, and
`X-Correlation-ID` values, stores them on `request.state`, and mirrors them to
the response headers. It does not log request bodies by default; enable
`include_request_body=True` only for routes where payload logging is safe.
`install_error_handlers()` formats route-level `HTTPException` and request
validation errors that FastAPI handles before middleware sees them.
`ErrorHandlingMiddleware` returns the shared `ApiResponse` error shape for infra
exceptions and unexpected errors that bubble through middleware, including the
trace id in both body and headers. If another custom middleware raises
`HTTPException`, install `ErrorHandlingMiddleware` outside it so the exception
can be formatted.
`SecurityHeadersMiddleware` adds conservative security headers. `CORSMiddleware`
defaults to public CORS without credentials; credentialed CORS must use explicit
origins because `allow_credentials=True` with `*` is rejected.

Application routes should use the same contract when they need an envelope:
`ApiResponse.ok(data, trace_id=...)` for success,
`ApiResponse.fail(ErrorCode.NOT_FOUND, "message", trace_id=...)` for errors, and
`PaginatedResponse.create(...)` with `PaginationParams` for page-number
pagination. `StandardResponse` and separate health response models are not part
of the public contract; health output comes from the observability routes.
