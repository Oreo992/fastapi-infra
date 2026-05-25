import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from infra.config.models import InfraSettings

DEFAULT_ENABLED_PLUGINS: tuple[str, ...] = ()
PROJECT_NAME_RE = re.compile(r"^[a-z](?:[a-z0-9_-]*[a-z0-9])?$")
PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ENV_EXAMPLE_DEFAULTS = {
    "JWT_SECRET": "replace-with-32-byte-random-secret",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "root",
    "MYSQL_PASSWORD": "local-password",
    "REDIS_URL": "redis://localhost:6379/0",
    "STRIPE_API_KEY": "sk_test_example",
    "STRIPE_WEBHOOK_SECRET": "whsec_example",
    "S3_LIVE_BUCKET": "bucket",
    "S3_LIVE_REGION": "us-east-1",
    "S3_LIVE_ACCESS_KEY_ID": "access-key",
    "S3_LIVE_SECRET_ACCESS_KEY": "secret-key",
    "S3_LIVE_ENDPOINT_URL": "https://s3.example.test",
    "SMTP_HOST": "smtp.example.test",
    "SMTP_PORT": "587",
    "SMTP_SENDER": "noreply@example.test",
    "SMTP_USERNAME": "smtp-user",
    "SMTP_PASSWORD": "smtp-password",
    "WEBHOOK_NOTIFICATION_HEALTH_URL": "",
    "WEBHOOK_NOTIFICATION_SIGNING_SECRET": "",
    "WEBHOOK_NOTIFICATION_URL": "",
}

PLUGIN_PROFILES: dict[str, tuple[str, ...]] = {
    "minimal": (),
    "api": (
        "auth",
        "database",
        "cache",
        "http",
        "observability",
        "ratelimit",
    ),
    "worker": (
        "database",
        "cache",
        "http",
        "tasks",
        "observability",
    ),
    "ai": (
        "ai",
        "speech",
        "database",
        "cache",
        "http",
        "observability",
    ),
    "saas": (
        "auth",
        "database",
        "cache",
        "http",
        "observability",
        "payment",
        "storage",
        "notifications",
        "webhooks",
        "ratelimit",
        "tasks",
    ),
}

PLUGIN_PROFILE_DESCRIPTIONS: dict[str, str] = {
    "minimal": "Core FastAPI project with every optional plugin explicitly disabled.",
    "api": "Typical web API foundation with auth, data, cache, HTTP, observability, and rate limits.",
    "worker": "Background processing foundation with data, cache, outbound HTTP, tasks, and observability.",
    "ai": "AI application foundation with model, speech, data, cache, HTTP, and observability plugins.",
    "saas": "SaaS foundation with auth, data, cache, payments, storage, notifications, webhooks, tasks, and limits.",
    "full": "Enable every built-in plugin for integration exploration.",
}


@lru_cache(maxsize=1)
def _plugin_manifest() -> dict[str, dict[str, object]]:
    from infra.plugins.builtin import get_builtin_plugins
    from infra.plugins.manager import PluginManager

    return PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins()).manifest()


def _plugin_manifest_for(plugin_registry: Iterable[Any] | None) -> dict[str, dict[str, object]]:
    if plugin_registry is None:
        return _plugin_manifest()
    from infra.plugins.manager import PluginManager

    return PluginManager(settings=InfraSettings(), plugins=list(plugin_registry)).manifest()


def _builtin_plugin_names() -> tuple[str, ...]:
    return tuple(_plugin_manifest())


class _BuiltinPluginNames:
    def __iter__(self):
        return iter(_builtin_plugin_names())

    def __len__(self) -> int:
        return len(_builtin_plugin_names())

    def __getitem__(self, index: int | slice) -> str | tuple[str, ...]:
        return _builtin_plugin_names()[index]

    def __eq__(self, other: object) -> bool:
        return tuple(self) == other

    def __repr__(self) -> str:
        return repr(tuple(self))


BUILTIN_PLUGIN_NAMES = _BuiltinPluginNames()


@dataclass
class _ScaffoldPlan:
    files: dict[Path, str]
    executable_paths: set[Path]


@dataclass(frozen=True)
class _ScaffoldContext:
    manifest: dict[str, dict[str, object]]
    plugin_names: tuple[str, ...]
    requested_plugins: tuple[str, ...]
    plugins: tuple[str, ...]
    production_plugins: tuple[str, ...]
    package_plugins: tuple[str, ...]


@dataclass(frozen=True)
class _RenderedScaffoldConfig:
    local_config: str
    production_config: str
    production_config_data: dict[str, Any]
    runtime_env_example: str
    provider_env_example: str


@dataclass(frozen=True)
class _MainRenderParts:
    stdlib_import_block: str
    fastapi_import: str
    plugin_import_block: str
    webhooks_lifespan: str
    fastapi_args: str
    post_setup_block: str
    route_block: str


def plugin_profiles() -> dict[str, tuple[str, ...]]:
    profiles = dict(PLUGIN_PROFILES)
    profiles["full"] = _builtin_plugin_names()
    return profiles


def plugin_profile_descriptions() -> dict[str, str]:
    return dict(PLUGIN_PROFILE_DESCRIPTIONS)


def plugins_for_profile(profile: str) -> tuple[str, ...]:
    normalized = profile.strip() or "minimal"
    profiles = plugin_profiles()
    try:
        return profiles[normalized]
    except KeyError as exc:
        available = ", ".join(sorted(profiles))
        raise ValueError(
            f"unknown plugin profile: {profile}. available profiles: {available}"
        ) from exc


def merge_profile_plugins(
    profile: str,
    extra_plugins: Iterable[str] = (),
) -> tuple[str, ...]:
    return _validate_plugins((*plugins_for_profile(profile), *tuple(extra_plugins)))


def create_project(
    destination: str | Path,
    project_name: str,
    enabled_plugins: Iterable[str] = DEFAULT_ENABLED_PLUGINS,
    profile: str = "minimal",
    overwrite: bool = False,
    plugin_registry: Iterable[Any] | None = None,
) -> list[Path]:
    """Create a small FastAPI project wired to this infrastructure package."""
    _validate_project_name(project_name)
    root = Path(destination)
    _validate_project_destination(root, overwrite=overwrite)
    plan = _build_scaffold_plan(
        project_name,
        enabled_plugins,
        profile=profile,
        plugin_registry=plugin_registry,
    )
    if overwrite:
        _remove_stale_optional_files(root, project_name, plan.files)
    return _write_scaffold_plan(root, plan)


def _validate_project_name(project_name: str) -> None:
    if not PROJECT_NAME_RE.fullmatch(project_name):
        raise ValueError(
            "project_name must start with a lowercase letter and contain only "
            "lowercase letters, numbers, underscores, or hyphens"
        )


def _validate_project_destination(root: Path, *, overwrite: bool) -> None:
    if root.exists() and not root.is_dir():
        raise FileExistsError(f"Destination exists and is not a directory: {root}")
    if root.exists() and any(root.iterdir()) and not overwrite:
        raise FileExistsError(f"Destination exists and is not empty: {root}")


def _build_scaffold_plan(
    project_name: str,
    enabled_plugins: Iterable[str],
    *,
    profile: str,
    plugin_registry: Iterable[Any] | None,
) -> _ScaffoldPlan:
    context = _build_scaffold_context(
        enabled_plugins,
        profile=profile,
        plugin_registry=plugin_registry,
    )
    rendered_config = _render_scaffold_config(project_name, context)
    files = _base_scaffold_files(project_name, profile, context, rendered_config)
    scaffold_files, executable_paths = _scaffold_files_for_plugins(
        context.plugins,
        manifest=context.manifest,
    )
    _merge_plugin_scaffold_files(files, scaffold_files)
    _add_optional_scaffold_files(project_name, files, context)
    return _ScaffoldPlan(files=files, executable_paths=executable_paths)


def _build_scaffold_context(
    enabled_plugins: Iterable[str],
    *,
    profile: str,
    plugin_registry: Iterable[Any] | None,
) -> _ScaffoldContext:
    manifest = _plugin_manifest_for(plugin_registry)
    plugin_names = tuple(manifest)
    profile_plugins = plugins_for_profile(profile)
    requested_plugins = tuple(enabled_plugins)
    plugins = _validate_plugins((*profile_plugins, *requested_plugins), plugin_names)
    production_plugins = _production_plugins_for(
        plugins,
        manifest=manifest,
        plugin_names=plugin_names,
    )
    package_plugins = tuple(dict.fromkeys((*plugins, *production_plugins)))
    return _ScaffoldContext(
        manifest=manifest,
        plugin_names=plugin_names,
        requested_plugins=requested_plugins,
        plugins=plugins,
        production_plugins=production_plugins,
        package_plugins=package_plugins,
    )


def _render_scaffold_config(
    project_name: str,
    context: _ScaffoldContext,
) -> _RenderedScaffoldConfig:
    production_overrides = _production_config_overrides(context.plugins)
    local_config = _render_infra_toml(
        context.plugins,
        "local_config_example",
        manifest=context.manifest,
        plugin_names=context.plugin_names,
    )
    production_config = _render_infra_toml(
        context.production_plugins,
        "production_config_example",
        env_references=True,
        config_overrides=production_overrides,
        manifest=context.manifest,
        plugin_names=context.plugin_names,
    )
    production_config_data = tomllib.loads(production_config)
    runtime_env_example = _render_env_example(
        project_name,
        context.production_plugins,
        manifest=context.manifest,
    )
    provider_env_example = _render_provider_env_example(
        production_config_data,
        runtime_env_example,
    )
    return _RenderedScaffoldConfig(
        local_config=local_config,
        production_config=production_config,
        production_config_data=production_config_data,
        runtime_env_example=runtime_env_example,
        provider_env_example=provider_env_example,
    )


def _base_scaffold_files(
    project_name: str,
    profile: str,
    context: _ScaffoldContext,
    rendered_config: _RenderedScaffoldConfig,
) -> dict[Path, str]:
    readme_sections = _scaffold_readme_sections_for_plugins(
        context.plugins,
        manifest=context.manifest,
    )
    migration_files = {
        **_app_migration_files_for_plugins(context.plugins),
        **_migration_files_for_plugins(
            context.production_plugins,
            manifest=context.manifest,
            plugin_names=context.plugin_names,
        ),
    }
    return {
        Path("AGENTS.md"): _render_agents_md(
            project_name,
            profile,
            context.plugins,
            context.production_plugins,
        ),
        Path(".github/workflows/ci.yml"): _render_project_ci_workflow(),
        Path(".dockerignore"): _render_dockerignore(),
        Path(".gitignore"): _render_gitignore(),
        Path("compose.yaml"): _render_compose_file(rendered_config.production_config_data),
        Path("pyproject.toml"): _render_pyproject(
            project_name,
            context.package_plugins,
            manifest=context.manifest,
        ),
        Path("app/main.py"): _render_main(
            project_name,
            context.plugins,
            context.package_plugins,
        ),
        Path("app/settings.py"): _render_settings(),
        Path("scripts/verify-release.sh"): _render_verify_release_script(
            context.production_plugins,
            provider_env_required=_provider_env_example_requires_credentials(
                rendered_config.provider_env_example
            ),
        ),
        Path("scripts/prepare-env.sh"): _render_prepare_env_script(),
        Path("tests/test_config.py"): _render_config_test(),
        Path("tests/test_health.py"): _render_health_test(
            context.plugins,
            manifest=context.manifest,
        ),
        Path("Dockerfile"): _render_dockerfile(context.production_plugins),
        Path("Makefile"): _render_makefile(context.production_plugins),
        Path("infra.manifest.json"): _render_project_manifest(
            project_name,
            profile,
            enabled_plugins=context.plugins,
            requested_plugins=context.requested_plugins,
            production_plugins=context.production_plugins,
            package_plugins=context.package_plugins,
            manifest=context.manifest,
        ),
        Path("README.md"): _render_readme(
            project_name,
            context.plugins,
            context.production_plugins,
            scaffold_readme_sections=readme_sections,
        ),
        Path(".env.example"): rendered_config.runtime_env_example,
        Path("provider.env.example"): rendered_config.provider_env_example,
        Path("infra.toml"): rendered_config.local_config,
        Path("infra.production.example.toml"): rendered_config.production_config,
        **migration_files,
    }


def _merge_plugin_scaffold_files(
    files: dict[Path, str],
    scaffold_files: Mapping[Path, str],
) -> None:
    for relative_path, content in scaffold_files.items():
        if relative_path in files:
            raise ValueError(f"plugin scaffold file conflicts with generated file: {relative_path}")
        files[relative_path] = content


def _add_optional_scaffold_files(
    project_name: str,
    files: dict[Path, str],
    context: _ScaffoldContext,
) -> None:
    if "tasks" in context.plugins:
        files[Path("app/worker.py")] = _render_worker(project_name, context.plugins)
    has_migrations = any(path.parts[0] == "migrations" for path in files)
    if "database" in context.production_plugins and not has_migrations:
        files[Path("migrations/.gitkeep")] = ""


def _write_scaffold_plan(root: Path, plan: _ScaffoldPlan) -> list[Path]:
    written: list[Path] = []
    for relative_path, content in plan.files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if path.suffix == ".sh" or relative_path in plan.executable_paths:
            path.chmod(0o755)
        written.append(path)
    return written


def _remove_stale_optional_files(
    root: Path,
    project_name: str,
    files_to_write: Mapping[Path, str],
) -> None:
    optional_files = {
        Path("app/worker.py"): _render_worker(project_name),
        Path("migrations/.gitkeep"): "",
        **_app_migration_files_for_plugins(_builtin_plugin_names()),
        **_migration_files_for_plugins(_builtin_plugin_names()),
    }
    for relative_path, generated_content in optional_files.items():
        if relative_path in files_to_write:
            continue
        path = root / relative_path
        if not path.is_file():
            continue
        if path.read_text(encoding="utf-8") != generated_content:
            continue
        path.unlink()
        _remove_empty_directory(path.parent, root)


def _remove_empty_directory(path: Path, stop: Path) -> None:
    if path == stop:
        return
    try:
        path.rmdir()
    except OSError:
        return


def _render_pyproject(
    project_name: str,
    enabled_plugins: Iterable[str],
    *,
    manifest: Mapping[str, Mapping[str, object]] | None = None,
) -> str:
    dependency = "fastapi-infra"
    extras = _extras_for_plugins(enabled_plugins, manifest=manifest)
    if extras:
        dependency = f"fastapi-infra[{','.join(extras)}]"

    return f"""[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "{project_name}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "{dependency}",
    "uvicorn[standard]>=0.29",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "httpx>=0.27.0,<0.29.0",
]
"""


def _render_main(
    project_name: str,
    enabled_plugins: Iterable[str],
    runtime_plugins: Iterable[str] | None = None,
) -> str:
    enabled = set(enabled_plugins)
    runtime_enabled = set(runtime_plugins or enabled)
    parts = _main_render_parts(project_name, enabled, runtime_enabled)
    return f"""{parts.stdlib_import_block}{parts.fastapi_import}

from infra import InfraSettings, setup_infra
from infra.middleware import (
    ErrorHandlingMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
    install_error_handlers,
)
{parts.plugin_import_block}
from .settings import build_settings


{parts.webhooks_lifespan}
app = FastAPI({parts.fastapi_args})
install_error_handlers(app)
app.add_middleware(ErrorHandlingMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

settings: InfraSettings = build_settings()
infra = setup_infra(app, settings)
{parts.post_setup_block}


@app.get("/health")
async def health() -> dict[str, object]:
    return {{
        name: status.model_dump()
        for name, status in infra.health.snapshot().items()
    }}
{parts.route_block}
"""


def _main_render_parts(
    project_name: str,
    enabled: set[str],
    runtime_enabled: set[str],
) -> _MainRenderParts:
    return _MainRenderParts(
        stdlib_import_block=_main_stdlib_import_block(enabled, runtime_enabled),
        fastapi_import=_main_fastapi_import(enabled),
        plugin_import_block=_main_plugin_import_block(enabled, runtime_enabled),
        webhooks_lifespan=_main_webhooks_lifespan(runtime_enabled),
        fastapi_args=_main_fastapi_args(project_name, runtime_enabled),
        post_setup_block=_main_post_setup_block(enabled),
        route_block=_main_route_block(enabled),
    )


def _main_stdlib_import_block(enabled: set[str], runtime_enabled: set[str]) -> str:
    imports: list[str] = []
    if "auth" in enabled:
        imports.append("from typing import Annotated")
    if "webhooks" in runtime_enabled:
        imports.extend(
            [
                "from collections.abc import AsyncIterator",
                "from contextlib import asynccontextmanager",
            ]
        )
    import_block = "\n".join(sorted(set(imports)))
    return f"{import_block}\n\n" if import_block else ""


def _main_fastapi_import(enabled: set[str]) -> str:
    imports = {"FastAPI"}
    if "auth" in enabled or "ratelimit" in enabled:
        imports.add("Depends")
    return f"from fastapi import {', '.join(sorted(imports))}"


def _main_plugin_import_block(enabled: set[str], runtime_enabled: set[str]) -> str:
    return "".join(
        (
            _render_plugin_service_import(_main_plugin_service_imports(enabled, runtime_enabled)),
            _main_auth_import(enabled),
            _main_ratelimit_import(enabled),
            _main_transaction_import(enabled),
            _main_observability_import(enabled),
            _main_webhooks_import(runtime_enabled),
        )
    )


def _main_plugin_service_imports(enabled: set[str], runtime_enabled: set[str]) -> list[str]:
    service_imports = [
        service for plugin, service in _MAIN_PLUGIN_SERVICE_IMPORTS.items() if plugin in enabled
    ]
    if "webhooks" in runtime_enabled:
        service_imports.extend(["DATABASE_SERVICE", "WEBHOOKS_SERVICE"])
    return service_imports


_MAIN_PLUGIN_SERVICE_IMPORTS = {
    "tasks": "TASKS_SERVICE",
    "payment": "PAYMENT_SERVICE",
    "notifications": "NOTIFICATIONS_SERVICE",
    "storage": "STORAGE_SERVICE",
    "ratelimit": "RATELIMIT_SERVICE",
    "cache": "CACHE_SERVICE",
    "database": "DATABASE_SERVICE",
    "http": "HTTP_SERVICE",
}


def _main_auth_import(enabled: set[str]) -> str:
    if "auth" not in enabled:
        return ""
    return "from infra.plugins.auth import Principal, require_principal\n"


def _main_ratelimit_import(enabled: set[str]) -> str:
    if "ratelimit" not in enabled:
        return ""
    return "from infra.plugins.ratelimit import rate_limit\n"


def _main_transaction_import(enabled: set[str]) -> str:
    if "database" not in enabled:
        return ""
    return "from infra.plugins.transaction.coordinator import Operation, TransactionCoordinator\n"


def _main_observability_import(enabled: set[str]) -> str:
    if "observability" not in enabled:
        return ""
    return (
        "from infra.plugins.observability import "
        "install_observability_middleware, install_observability_routes\n"
    )


def _main_webhooks_import(runtime_enabled: set[str]) -> str:
    if "webhooks" not in runtime_enabled:
        return ""
    return "from infra.plugins.webhooks import SqlWebhookStore, install_webhook_routes\n"


def _main_webhooks_lifespan(runtime_enabled: set[str]) -> str:
    if "webhooks" not in runtime_enabled:
        return ""
    return """@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    webhooks = infra.get(WEBHOOKS_SERVICE)
    if webhooks is not None and not getattr(app.state, "webhook_routes_installed", False):
        database = infra.get(DATABASE_SERVICE)
        store = SqlWebhookStore(database) if database is not None else None
        install_webhook_routes(app, webhooks, store=store)
        app.state.webhook_routes_installed = True
    yield

"""


def _main_fastapi_args(project_name: str, runtime_enabled: set[str]) -> str:
    if "webhooks" in runtime_enabled:
        return f'title="{project_name}", lifespan=lifespan'
    return f'title="{project_name}"'


def _main_post_setup_block(enabled: set[str]) -> str:
    if "observability" not in enabled:
        return ""
    return """
install_observability_middleware(app)
install_observability_routes(app, infra, prefix="/ops")
"""


def _main_route_block(enabled: set[str]) -> str:
    database_routes = _render_main_database_routes() if "database" in enabled else ""
    route_blocks = [
        block.strip()
        for block in (
            _render_main_static_routes(enabled, "auth"),
            _render_main_static_routes(enabled, "cache"),
            database_routes,
            _render_main_static_routes(enabled, "http"),
            _render_main_static_routes(enabled, "ratelimit"),
            _render_main_static_routes(enabled, "notifications"),
            _render_main_static_routes(enabled, "payment"),
            _render_main_static_routes(enabled, "storage"),
            _render_main_static_routes(enabled, "tasks"),
        )
        if block.strip()
    ]
    route_block = "\n\n".join(route_blocks)
    return f"\n\n{route_block}\n" if route_block else ""


def _render_main_database_routes() -> str:
    return """

@app.post("/database/example")
async def write_example_document() -> dict[str, object]:
    database = infra.require(DATABASE_SERVICE)
    return await database.put_document(
        "examples",
        "greeting",
        {"message": "hello from fastapi-infra"},
    )


@app.get("/database/example")
async def read_example_document() -> dict[str, object]:
    database = infra.require(DATABASE_SERVICE)
    document = await database.get_document("examples", "greeting")
    return {"document": document}


@app.post("/transactions/example")
async def run_example_transaction(fail: bool = False) -> dict[str, object]:
    database = infra.require(DATABASE_SERVICE)
    key = "failure" if fail else "success"

    async def create_order() -> dict[str, object]:
        return await database.put_document("orders", key, {"status": "created"})

    async def cancel_order() -> None:
        await database.delete_document("orders", key)

    async def write_audit_log() -> dict[str, object]:
        if fail:
            raise RuntimeError("audit write failed")
        return await database.put_document("audit", key, {"event": "order_created"})

    result = await TransactionCoordinator().execute_with_compensation(
        [
            Operation(name="create_order", execute=create_order, compensate=cancel_order),
            Operation(name="write_audit_log", execute=write_audit_log),
        ]
    )
    return {
        "success": result.success,
        "completed_operations": result.completed_operations,
        "failed_operation": result.failed_operation,
        "compensated_operations": result.compensated_operations,
        "order": await database.get_document("orders", key),
        "audit": await database.get_document("audit", key),
    }
"""


def _render_main_static_routes(enabled_plugins: set[str], plugin: str) -> str:
    if plugin not in enabled_plugins:
        return ""
    return _MAIN_STATIC_ROUTE_SECTIONS[plugin]


_MAIN_STATIC_ROUTE_SECTIONS = {
    "auth": """

@app.get("/me")
async def me(principal: Annotated[Principal, Depends(require_principal)]) -> dict[str, object]:
    return {
        "subject": principal.subject,
        "scopes": sorted(principal.scopes),
        "roles": sorted(principal.roles),
    }
""",
    "cache": """

@app.post("/cache/example")
async def write_example_cache_value() -> dict[str, object]:
    cache = infra.require(CACHE_SERVICE)
    await cache.set("examples:greeting", {"message": "hello from fastapi-infra"}, ttl=60)
    return {"key": "examples:greeting", "stored": True}


@app.get("/cache/example")
async def read_example_cache_value() -> dict[str, object]:
    cache = infra.require(CACHE_SERVICE)
    value = await cache.get("examples:greeting")
    return {"key": "examples:greeting", "value": value}
""",
    "http": """

@app.get("/http/example")
async def call_example_http_service() -> dict[str, object]:
    http = infra.require(HTTP_SERVICE)
    response = await http.request(
        "GET",
        "/example",
        headers={"X-Example": "fastapi-infra"},
    )
    return {
        "status_code": response.status_code,
        "url": response.url,
        "body": response.json(),
    }
""",
    "notifications": """

@app.post("/notifications/example")
async def send_example_notification() -> dict[str, object]:
    notifications = infra.require(NOTIFICATIONS_SERVICE)
    result = await notifications.send(
        channel="email",
        recipient="user@example.test",
        subject="Hello from fastapi-infra",
        body="This notification was sent through the configured provider.",
        metadata={"source": "api"},
    )
    return result.model_dump()
""",
    "payment": """

@app.post("/payments/example")
async def create_example_checkout() -> dict[str, object]:
    payment = infra.require(PAYMENT_SERVICE)
    checkout = await payment.create_checkout(
        amount=1999,
        currency="usd",
        reference="example-checkout",
        success_url="https://example.test/success",
        cancel_url="https://example.test/cancel",
    )
    return checkout.model_dump()
""",
    "ratelimit": """

@app.get(
    "/limited",
    dependencies=[
        Depends(rate_limit(limit=2, window_seconds=60, service=RATELIMIT_SERVICE))
    ],
)
async def limited() -> dict[str, str]:
    return {"status": "ok"}
""",
    "storage": """

@app.post("/storage/example")
async def write_example_object() -> dict[str, object]:
    storage = infra.require(STORAGE_SERVICE)
    key = "examples/hello.txt"
    await storage.put_object(key, b"hello from fastapi-infra", content_type="text/plain")
    return {"key": key, "exists": await storage.exists(key)}


@app.get("/storage/example")
async def read_example_object() -> dict[str, object]:
    storage = infra.require(STORAGE_SERVICE)
    key = "examples/hello.txt"
    data = await storage.get_object(key)
    return {"key": key, "content": data.decode("utf-8")}
""",
    "tasks": """

@app.post("/tasks/example")
async def enqueue_example_task() -> dict[str, object]:
    queue = infra.require(TASKS_SERVICE)
    task = await queue.enqueue("example.ping", {"source": "api"})
    return {"id": task.id, "name": task.name, "state": task.state}
""",
}


def _render_worker(
    project_name: str,
    enabled_plugins: Iterable[str] = (),
) -> str:
    enabled = set(enabled_plugins)
    service_imports = ["TASKS_SERVICE"]
    instrumentation_setup = ""
    worker_args = "queue"
    if "observability" in enabled:
        service_imports.append("OBSERVABILITY_SERVICE")
        instrumentation_setup = """
    instrumentation = infra.get(OBSERVABILITY_SERVICE)
"""
        worker_args = "queue, instrumentation=instrumentation"
    return f"""import asyncio

from fastapi import FastAPI

from infra import InfraSettings, setup_infra
{_render_plugin_service_import(service_imports).rstrip()}
from infra.plugins.tasks import (
    TaskEnvelope,
    TaskWorker,
    TaskWorkerRunConfig,
    run_task_worker,
)

from .settings import build_settings


app = FastAPI(title="{project_name} worker")
settings: InfraSettings = build_settings()
infra = setup_infra(app, settings)


def build_worker() -> TaskWorker:
    queue = infra.require(TASKS_SERVICE)
{instrumentation_setup}    worker = TaskWorker({worker_args})

    @worker.handler("example.ping")
    async def handle_example_ping(task: TaskEnvelope) -> None:
        print(f"received example.ping {{task.id}}: {{task.payload}}")

    return worker


async def run() -> None:
    await infra.startup()
    try:
        worker = build_worker()
        await run_task_worker(
            worker,
            TaskWorkerRunConfig(idle_sleep=0.5, require_handlers=True),
        )
    finally:
        await infra.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
"""


def _render_plugin_service_import(names: Iterable[str]) -> str:
    imports = sorted(set(names))
    if not imports:
        return ""
    return f"from infra.plugins import {', '.join(imports)}\n"


def _render_health_test(
    enabled_plugins: Iterable[str] = DEFAULT_ENABLED_PLUGINS,
    *,
    manifest: Mapping[str, Mapping[str, object]] | None = None,
) -> str:
    expected_services = _expected_services_for_plugins(enabled_plugins, manifest=manifest)
    enabled = set(enabled_plugins)
    plugin_service_import = _render_plugin_service_import(_health_test_service_imports(enabled))
    auth_tests = _render_static_health_tests(enabled, "auth")
    notifications_tests = _render_static_health_tests(enabled, "notifications")
    payment_tests = _render_static_health_tests(enabled, "payment")
    cache_tests = _render_static_health_tests(enabled, "cache")
    database_tests = _render_database_health_tests(enabled)
    http_tests = _render_static_health_tests(enabled, "http")
    storage_tests = _render_static_health_tests(enabled, "storage")
    ratelimit_tests = _render_static_health_tests(enabled, "ratelimit")
    tasks_tests = _render_tasks_health_tests(enabled)
    return f"""import importlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

{plugin_service_import}

main = importlib.import_module("app.main")
app = main.app
infra = main.infra

EXPECTED_SERVICES = {expected_services!r}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def test_health_returns_snapshot(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_health_includes_trace_headers(client: TestClient) -> None:
    response = client.get(
        "/health",
        headers={{
            "X-Trace-ID": "trace-from-test",
            "X-Request-ID": "request-from-test",
        }},
    )

    assert response.status_code == 200
    assert response.headers["X-Trace-ID"] == "trace-from-test"
    assert response.headers["X-Request-ID"] == "request-from-test"


def test_health_includes_security_headers(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_enabled_plugin_services_are_registered(client: TestClient) -> None:
    for service_name in EXPECTED_SERVICES:
        assert infra.get(service_name) is not None
{auth_tests}
{cache_tests}
{database_tests}
{http_tests}
{ratelimit_tests}
{notifications_tests}
{payment_tests}
{storage_tests}
{tasks_tests}
"""


def _health_test_service_imports(enabled_plugins: set[str]) -> list[str]:
    service_imports = {
        "auth": "AUTH_SERVICE",
        "cache": "CACHE_SERVICE",
        "database": "DATABASE_SERVICE",
        "http": "HTTP_SERVICE",
        "notifications": "NOTIFICATIONS_SERVICE",
        "payment": "PAYMENT_SERVICE",
        "ratelimit": "RATELIMIT_SERVICE",
        "observability": "OBSERVABILITY_SERVICE",
        "tasks": "TASKS_SERVICE",
    }
    return [service for plugin, service in service_imports.items() if plugin in enabled_plugins]


def _render_static_health_tests(enabled_plugins: set[str], plugin: str) -> str:
    if plugin not in enabled_plugins:
        return ""
    return _STATIC_HEALTH_TEST_SECTIONS[plugin]


_STATIC_HEALTH_TEST_SECTIONS = {
    "auth": """

def test_auth_me_requires_bearer_token(client: TestClient) -> None:
    response = client.get("/me")

    assert response.status_code == 401


def test_auth_me_accepts_issued_jwt(client: TestClient) -> None:
    auth = infra.require(AUTH_SERVICE)
    token = auth.issue_jwt(
        subject="user-1",
        scopes={"profile:read"},
        roles={"user"},
    )

    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "subject": "user-1",
        "scopes": ["profile:read"],
        "roles": ["user"],
    }
""",
    "cache": """

def test_cache_example_routes_write_and_read_value(client: TestClient) -> None:
    before_write = client.get("/cache/example")

    assert before_write.status_code == 200
    assert before_write.json() == {"key": "examples:greeting", "value": None}

    write_response = client.post("/cache/example")

    assert write_response.status_code == 200
    assert write_response.json() == {"key": "examples:greeting", "stored": True}

    read_response = client.get("/cache/example")

    assert read_response.status_code == 200
    assert read_response.json() == {
        "key": "examples:greeting",
        "value": {"message": "hello from fastapi-infra"},
    }
    assert infra.require(CACHE_SERVICE) is not None
""",
    "http": """

def test_http_example_route_uses_configured_client(client: TestClient) -> None:
    response = client.get("/http/example")

    assert response.status_code == 200
    assert response.json() == {
        "status_code": 200,
        "url": "mock://http/example",
        "body": {
            "ok": True,
            "request": {"method": "GET", "url": "mock://http/example"},
        },
    }
    http = infra.require(HTTP_SERVICE)
    assert http.requests[-1]["headers"] == {"X-Example": "fastapi-infra"}
""",
    "notifications": """

def test_notifications_example_route_sends_message(client: TestClient) -> None:
    response = client.post("/notifications/example")

    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("ntf_")
    assert body["channel"] == "email"
    assert body["recipient"] == "user@example.test"
    assert body["subject"] == "Hello from fastapi-infra"
    assert body["metadata"] == {"source": "api"}
    assert body["status"] == "skipped"
    assert infra.require(NOTIFICATIONS_SERVICE).get().results[-1].id == body["id"]
""",
    "payment": """

def test_payment_example_route_creates_checkout(client: TestClient) -> None:
    response = client.post("/payments/example")

    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("chk_mock_")
    assert body["amount"] == 1999
    assert body["currency"] == "USD"
    assert body["reference"] == "example-checkout"
    assert body["status"] == "pending"
    assert body["url"].startswith("mock://checkout/")
    status = awaitable_result(infra.require(PAYMENT_SERVICE).get_payment_status(body["id"]))
    assert status == "pending"


def awaitable_result(value):
    import asyncio

    return asyncio.run(value)
""",
    "ratelimit": """

def test_rate_limited_route_blocks_after_limit(client: TestClient) -> None:
    first = client.get("/limited")
    second = client.get("/limited")
    blocked = client.get("/limited")

    assert first.status_code == 200
    assert second.status_code == 200
    assert blocked.status_code == 429
    assert blocked.json()["error"]["message"] == "rate limit exceeded"
    assert blocked.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert blocked.headers["X-RateLimit-Limit"] == "2"
    assert blocked.headers["Retry-After"] == "60"
""",
    "storage": """

def test_storage_example_routes_write_and_read_object(client: TestClient) -> None:
    write_response = client.post("/storage/example")

    assert write_response.status_code == 200
    assert write_response.json() == {"key": "examples/hello.txt", "exists": True}

    read_response = client.get("/storage/example")

    assert read_response.status_code == 200
    assert read_response.json() == {
        "key": "examples/hello.txt",
        "content": "hello from fastapi-infra",
    }
""",
}


def _render_database_health_tests(enabled_plugins: set[str]) -> str:
    if "database" not in enabled_plugins:
        return ""
    return """

def test_database_example_routes_write_and_read_document(client: TestClient) -> None:
    before_write = client.get("/database/example")

    assert before_write.status_code == 200
    assert before_write.json() == {"document": None}

    write_response = client.post("/database/example")

    assert write_response.status_code == 200
    assert write_response.json() == {
        "collection": "examples",
        "key": "greeting",
        "value": {"message": "hello from fastapi-infra"},
    }

    read_response = client.get("/database/example")

    assert read_response.status_code == 200
    assert read_response.json() == {
        "document": {
            "collection": "examples",
            "key": "greeting",
            "value": {"message": "hello from fastapi-infra"},
        }
    }
    assert infra.require(DATABASE_SERVICE) is not None


def test_transaction_example_route_reports_success_and_compensation(client: TestClient) -> None:
    success_response = client.post("/transactions/example")

    assert success_response.status_code == 200
    assert success_response.json() == {
        "success": True,
        "completed_operations": ["create_order", "write_audit_log"],
        "failed_operation": None,
        "compensated_operations": [],
        "order": {
            "collection": "orders",
            "key": "success",
            "value": {"status": "created"},
        },
        "audit": {
            "collection": "audit",
            "key": "success",
            "value": {"event": "order_created"},
        },
    }

    failure_response = client.post("/transactions/example?fail=true")

    assert failure_response.status_code == 200
    assert failure_response.json() == {
        "success": False,
        "completed_operations": ["create_order"],
        "failed_operation": "write_audit_log",
        "compensated_operations": ["create_order"],
        "order": None,
        "audit": None,
    }
"""


def _render_tasks_health_tests(enabled_plugins: set[str]) -> str:
    if "tasks" not in enabled_plugins:
        return ""
    task_observability_capture = "            return stats, stored"
    task_observability_assertions = ""
    task_result_unpack = "stats, task"
    if "observability" in enabled_plugins:
        task_observability_capture = """            observability = worker_module.infra.require(OBSERVABILITY_SERVICE)
            counters = dict(observability.counters)
            timers = {
                name: list(values)
                for name, values in observability.timers.items()
            }
            return stats, stored, counters, timers"""
        task_result_unpack = "stats, task, counters, timers"
        task_observability_assertions = """
    assert counters["task_worker_tasks_total"] == 1
    assert counters["task_worker_completed_total"] == 1
    assert timers["task_worker_task_seconds"]
"""
    tasks_tests = """

def test_tasks_example_route_enqueues_task(client: TestClient) -> None:
    response = client.post("/tasks/example")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "example.ping"
    assert body["state"] == "queued"
    task = infra.require(TASKS_SERVICE).get(body["id"])
    assert task.payload == {"source": "api"}


def test_task_worker_processes_example_task_once() -> None:
    import asyncio

    worker_module = importlib.import_module("app.worker")

    async def scenario():
        await worker_module.infra.startup()
        try:
            queue = worker_module.infra.require(TASKS_SERVICE)
            task = await queue.enqueue("example.ping", {"source": "worker-test"})
            worker = worker_module.build_worker()
            stats = await worker.run(
                worker_module.TaskWorkerRunConfig(
                    max_tasks=1,
                    idle_poll_limit=1,
                    require_handlers=True,
                )
            )
            stored = queue.get(task.id)
__TASK_OBSERVABILITY_CAPTURE__
        finally:
            await worker_module.infra.shutdown()

    __TASK_RESULT_UNPACK__ = asyncio.run(scenario())

    assert stats.processed == 1
    assert stats.completed == 1
    assert stats.retried == 0
    assert stats.dead_lettered == 0
    assert task.name == "example.ping"
    assert task.state == "completed"
"""
    return (
        tasks_tests.replace("__TASK_OBSERVABILITY_CAPTURE__", task_observability_capture).replace(
            "__TASK_RESULT_UNPACK__", task_result_unpack
        )
        + task_observability_assertions
    )


def _expected_services_for_plugins(
    enabled_plugins: Iterable[str],
    *,
    manifest: Mapping[str, Mapping[str, object]] | None = None,
) -> list[str]:
    resolved_manifest = manifest or _plugin_manifest()
    services: set[str] = set()
    for plugin in enabled_plugins:
        item = resolved_manifest.get(plugin, {})
        provides = item.get("provides", [])
        if isinstance(provides, list):
            services.update(service for service in provides if isinstance(service, str))
        service_name_config = item.get("service_name_config")
        config = item.get("local_config_example", {})
        if isinstance(service_name_config, str) and isinstance(config, Mapping):
            configured_service = config.get(service_name_config)
            if isinstance(configured_service, str):
                services.add(configured_service)
    return sorted(services)


def _render_config_test() -> str:
    return """from pathlib import Path
import subprocess
import sys

from infra.config import load_infra_settings, validate_infra_settings
from infra.plugins.discovery import get_available_plugins


ROOT = Path(__file__).resolve().parents[1]


def test_local_infra_config_loads_and_validates() -> None:
    settings = load_infra_settings(ROOT / "infra.toml")

    assert validate_infra_settings(settings, get_available_plugins(settings)) == []


def test_local_infra_config_passes_cli_config_check() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "infra.cli",
            "config-check",
            "--settings",
            str(ROOT / "infra.toml"),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "config-check: valid" in result.stdout


def test_production_config_example_is_present() -> None:
    assert (ROOT / "infra.production.example.toml").exists()
    assert (ROOT / ".env.example").exists()
    assert (ROOT / "provider.env.example").exists()


def test_production_config_example_passes_cli_config_check() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "infra.cli",
            "config-check",
            "--settings",
            str(ROOT / "infra.production.example.toml"),
            "--env-file",
            str(ROOT / ".env.example"),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "config-check: valid" in result.stdout
"""


def _render_verify_release_script(
    production_plugins: Iterable[str],
    *,
    provider_env_required: bool,
) -> str:
    production = set(production_plugins)
    migrations_arg = " --migrations migrations" if "database" in production else ""
    provider_env_check = ""
    provider_env_arg = ""
    runtime_env_check = ""
    if "auth" in production:
        runtime_env_check = """
if grep -Eq '^JWT_SECRET=(change-me|changeme|dev-secret|dev-secret-change-me|jwt-secret|password|replace-with-32-byte-random-secret|secret|test|test-secret)$' "$RUNTIME_ENV_FILE"; then
  echo "unsafe JWT_SECRET in $RUNTIME_ENV_FILE; replace it with a random secret of at least 32 characters before running release checks" >&2
  exit 1
fi
"""
    if provider_env_required:
        provider_env_arg = ' --env-file "$PROVIDER_ENV_FILE"'
        provider_env_check = """
if [ ! -f "$PROVIDER_ENV_FILE" ]; then
  echo "missing $PROVIDER_ENV_FILE; copy provider.env.example to provider.env and fill live provider credentials" >&2
  exit 1
fi
"""
    return f"""#!/usr/bin/env sh
set -eu

RUNTIME_ENV_FILE="${{1:-.env}}"
PROVIDER_ENV_FILE="${{2:-provider.env}}"
PROVIDER_CHECK="${{PROVIDER_CHECK:-${{3:-}}}}"

echo "== local verification =="
make verify

if [ ! -f "$RUNTIME_ENV_FILE" ]; then
  echo "missing $RUNTIME_ENV_FILE; copy .env.example to .env and fill runtime credentials" >&2
  exit 1
fi
{runtime_env_check}

echo "== static release gate =="
RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" make release-static

echo "== required provider checks =="
RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" make provider-list

{provider_env_check}
echo "== provider preflight =="
if [ -f "$PROVIDER_ENV_FILE" ]; then
  RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" PROVIDER_ENV_FILE="$PROVIDER_ENV_FILE" make provider-preflight
else
  fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file "$RUNTIME_ENV_FILE" --preflight
fi

if [ "${{RUN_LIVE_CERTIFICATION:-0}}" = "1" ]; then
  echo "== live provider certification =="
  fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file "$RUNTIME_ENV_FILE"{provider_env_arg} --json > provider-certification.json
  fastapi-infra release-check --settings infra.production.example.toml --env-file "$RUNTIME_ENV_FILE"{migrations_arg} --provider-certification-report provider-certification.json
elif [ -n "$PROVIDER_CHECK" ]; then
  if [ ! -f "$PROVIDER_ENV_FILE" ]; then
    echo "missing $PROVIDER_ENV_FILE; copy provider.env.example to provider.env and fill live provider credentials" >&2
    exit 1
  fi
  echo "== extra provider preflight: $PROVIDER_CHECK =="
  fastapi-infra certify-providers --provider "$PROVIDER_CHECK" --preflight --env-file "$PROVIDER_ENV_FILE"
else
  echo "set RUN_LIVE_CERTIFICATION=1 to run live provider tests"
fi
"""


def _render_prepare_env_script() -> str:
    return """#!/usr/bin/env sh
set -eu

RUNTIME_ENV_FILE="${1:-.env}"
PROVIDER_ENV_FILE="${2:-provider.env}"

if [ ! -f "$RUNTIME_ENV_FILE" ]; then
  cp .env.example "$RUNTIME_ENV_FILE"
fi

if grep -Eq '^JWT_SECRET=(change-me|changeme|dev-secret|dev-secret-change-me|jwt-secret|password|replace-with-32-byte-random-secret|secret|test|test-secret)$' "$RUNTIME_ENV_FILE"; then
  RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" python - <<'PY'
from pathlib import Path
import os
import secrets

path = Path(os.environ["RUNTIME_ENV_FILE"])
lines = path.read_text(encoding="utf-8").splitlines()
secret = secrets.token_urlsafe(32)
updated = []
for line in lines:
    if line.startswith("JWT_SECRET="):
        updated.append(f"JWT_SECRET={secret}")
    else:
        updated.append(line)
path.write_text("\\n".join(updated) + "\\n", encoding="utf-8")
PY
fi

if [ ! -f "$PROVIDER_ENV_FILE" ]; then
  cp provider.env.example "$PROVIDER_ENV_FILE"
fi

echo "prepared $RUNTIME_ENV_FILE and $PROVIDER_ENV_FILE"
"""


def _render_dockerfile(production_plugins: Iterable[str] = ()) -> str:
    migrations_copy = ""
    if "database" in set(production_plugins):
        migrations_copy = "COPY migrations ./migrations\n"
    return f"""FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV INFRA_SETTINGS=infra.toml

WORKDIR /app

COPY pyproject.toml README.md infra.toml infra.production.example.toml infra.manifest.json ./
COPY app ./app
COPY scripts ./scripts
{migrations_copy}

RUN pip install --no-cache-dir . \\
    && chmod +x scripts/*.sh \\
    && adduser --disabled-password --gecos "" appuser \\
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \\
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""


def _render_makefile(production_plugins: Iterable[str] = ()) -> str:
    migrations_arg = " --migrations migrations" if "database" in set(production_plugins) else ""
    return f""".PHONY: install run test config-check project-check verify env release-static provider-list provider-preflight release dev-up dev-down docker-build

RUNTIME_ENV_FILE ?= .env
PROVIDER_ENV_FILE ?= provider.env

install:
\tpip install -e ".[dev]"

run:
\tuvicorn app.main:app --reload

test:
\tpython -m pytest -q

config-check:
\tfastapi-infra config-check --settings infra.toml

project-check:
\tfastapi-infra project-check .

verify: config-check project-check test

env:
\tscripts/prepare-env.sh $(RUNTIME_ENV_FILE) $(PROVIDER_ENV_FILE)

release-static:
\tfastapi-infra config-check --settings infra.production.example.toml --env-file $(RUNTIME_ENV_FILE)
\tfastapi-infra release-check --settings infra.production.example.toml --env-file $(RUNTIME_ENV_FILE){migrations_arg} --static-only

provider-list:
\tfastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file $(RUNTIME_ENV_FILE) --list --requirements

provider-preflight:
\tfastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file $(RUNTIME_ENV_FILE) --preflight --env-file $(PROVIDER_ENV_FILE)

release:
\tscripts/verify-release.sh $(RUNTIME_ENV_FILE) $(PROVIDER_ENV_FILE)

dev-up:
\tdocker compose up --build

dev-down:
\tdocker compose down

docker-build:
\tdocker build -t app .
"""


def _render_dockerignore() -> str:
    return """.venv
__pycache__
.pytest_cache
.mypy_cache
dist
*.egg-info
*.pyc
.env
provider.env
provider-env-template.env
provider-certification.json
provider-preflight.json
"""


def _render_gitignore() -> str:
    return """.venv
__pycache__
.pytest_cache
.mypy_cache
.coverage
htmlcov
dist
*.egg-info
*.pyc
.env
provider.env
provider-env-template.env
provider-certification.json
provider-preflight.json
"""


def _render_compose_file(production_config: Mapping[str, Any]) -> str:
    dependencies = _compose_dependencies_for_production_config(production_config)
    dependency_lines = []
    app_environment = [
        "      INFRA_SETTINGS: infra.production.example.toml",
    ]
    services = []
    volumes = []
    if dependencies["mysql"]:
        dependency_lines.extend(
            [
                "      mysql:",
                "        condition: service_healthy",
            ]
        )
        app_environment.append("      MYSQL_HOST: mysql")
        services.append("""  mysql:
    image: mysql:8.4
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_PASSWORD:-local-password}
      MYSQL_DATABASE: ${MYSQL_DATABASE:-app}
    ports:
      - "3306:3306"
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uroot -p$${MYSQL_ROOT_PASSWORD} --silent"]
      interval: 10s
      timeout: 5s
      retries: 10
    volumes:
      - mysql_data:/var/lib/mysql
""")
        volumes.append("  mysql_data:\n")
    if dependencies["redis"]:
        dependency_lines.extend(
            [
                "      redis:",
                "        condition: service_healthy",
            ]
        )
        app_environment.append("      REDIS_URL: redis://redis:6379/0")
        services.append("""  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 10
    volumes:
      - redis_data:/data
""")
        volumes.append("  redis_data:\n")
    depends_on = ""
    if dependency_lines:
        depends_on = "    depends_on:\n" + "\n".join(dependency_lines) + "\n"
    services_block = "".join(services)
    volumes_block = ""
    if volumes:
        volumes_block = "\nvolumes:\n" + "".join(volumes)
    return f"""services:
  app:
    build: .
    env_file:
      - .env
    environment:
{chr(10).join(app_environment)}
    ports:
      - "8000:8000"
{depends_on}{services_block}{volumes_block}"""


def _compose_dependencies_for_production_config(
    production_config: Mapping[str, Any],
) -> dict[str, bool]:
    plugins = production_config.get("infra", {}).get("plugins", {})
    if not isinstance(plugins, Mapping):
        return {"mysql": False, "redis": False}
    database_plugin = plugins.get("database", {})
    database_config: object = {}
    if isinstance(database_plugin, Mapping):
        plugin_config = database_plugin.get("config", {})
        if isinstance(plugin_config, Mapping):
            database_config = plugin_config.get("config", {})
    mysql = isinstance(database_config, Mapping) and database_config.get("mysql_enabled") is True
    redis = isinstance(database_config, Mapping) and database_config.get("redis_enabled") is True
    cache_plugin = plugins.get("cache", {})
    if isinstance(cache_plugin, Mapping):
        cache_config = cache_plugin.get("config", {})
        if isinstance(cache_config, Mapping) and cache_config.get("default_provider") == "redis":
            redis = True
    return {"mysql": mysql, "redis": redis}


def _render_settings() -> str:
    return """import os
from pathlib import Path

from infra import InfraSettings
from infra.config import load_infra_settings


CONFIG_PATH = Path(__file__).resolve().parents[1] / "infra.toml"


def build_settings() -> InfraSettings:
    config_path = Path(os.environ.get("INFRA_SETTINGS", CONFIG_PATH))
    return load_infra_settings(config_path)
"""


def _render_readme(
    project_name: str,
    enabled_plugins: Iterable[str],
    production_plugins: Iterable[str] | None = None,
    *,
    scaffold_readme_sections: Iterable[str] = (),
) -> str:
    plugins = tuple(enabled_plugins)
    production_plugin_tuple = tuple(production_plugins or plugins)
    plugin_list = ", ".join(plugins) or "none"
    production_migrations_arg = (
        " --migrations migrations" if "database" in production_plugin_tuple else ""
    )
    production_profile_block = _readme_production_profile_block(
        plugins,
        production_plugin_tuple,
    )
    migrations_block = _readme_migrations_section(plugins)
    worker_block = _readme_worker_section(plugins)
    plugin_sections = _readme_plugin_sections(scaffold_readme_sections)
    return "".join(
        (
            _readme_intro_section(project_name),
            _readme_local_configuration_section(),
            _readme_production_check_section(production_migrations_arg),
            _readme_docker_section(project_name),
            _readme_configure_section(plugin_list, production_profile_block),
            migrations_block,
            worker_block,
            plugin_sections,
        )
    )


def _readme_intro_section(project_name: str) -> str:
    return f"""# {project_name}

Small FastAPI app generated from `fastapi-infra`.

## Install

```bash
pip install -e ".[dev]"
make verify
```

## Run

```bash
uvicorn app.main:app --reload
make run
```

## Verify

```bash
make verify
fastapi-infra config-check --settings infra.toml
fastapi-infra project-check .
python -m pytest -q
```
"""


def _readme_local_configuration_section() -> str:
    return """
## Local Configuration

`infra.toml` is the local profile. `.env.example` lists runtime variables used by the enabled production providers.

```bash
make env
make config-check
fastapi-infra plugins --settings infra.toml
fastapi-infra config-check --settings infra.toml
```
"""


def _readme_production_check_section(production_migrations_arg: str) -> str:
    return f"""
## Production Check

`infra.production.example.toml` enables production-oriented provider settings. `make env` creates `.env` and `provider.env`; for auth profiles it also replaces the unsafe example `JWT_SECRET` with a generated local secret. Keep live provider certification credentials in `provider.env`.

```bash
make env
make release-static
make provider-preflight
scripts/verify-release.sh .env provider.env
fastapi-infra config-check --settings infra.production.example.toml --env-file .env
fastapi-infra release-check --settings infra.production.example.toml --env-file .env{production_migrations_arg} --static-only
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --list --requirements
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --preflight --env-file provider.env
```
"""


def _readme_docker_section(project_name: str) -> str:
    return f"""
## Docker

```bash
make dev-up
docker build -t {project_name} .
docker compose up --build
docker run --rm -p 8000:8000 {project_name}
docker run --rm -p 8000:8000 --env-file .env -e INFRA_SETTINGS=infra.production.example.toml {project_name}
```
"""


def _readme_configure_section(plugin_list: str, production_profile_block: str) -> str:
    return f"""
## Configure

```bash
make project-check
fastapi-infra profiles
fastapi-infra plugins --settings infra.toml
fastapi-infra config-check --settings infra.toml
fastapi-infra project-check . --json
```

Enabled plugins: {plugin_list}
{production_profile_block}
"""


def _readme_production_profile_block(
    enabled_plugins: tuple[str, ...],
    production_plugins: tuple[str, ...],
) -> str:
    if production_plugins == enabled_plugins:
        return ""
    production_plugin_list = ", ".join(production_plugins) or "none"
    return "\nProduction profile plugins: " f"{production_plugin_list}\n"


def _readme_migrations_section(enabled_plugins: tuple[str, ...]) -> str:
    if "database" not in enabled_plugins:
        return ""
    return """
## Database migrations

```bash
fastapi-infra migrations new migrations create_users
fastapi-infra migrations list migrations
fastapi-infra migrations migrate migrations --settings infra.toml
```
"""


def _readme_worker_section(enabled_plugins: tuple[str, ...]) -> str:
    if "tasks" not in enabled_plugins:
        return ""
    return """
## Worker

```bash
python -m app.worker
```
"""


def _readme_plugin_sections(scaffold_readme_sections: Iterable[str]) -> str:
    plugin_sections = "\n".join(section.rstrip() for section in scaffold_readme_sections if section)
    if not plugin_sections:
        return ""
    return "\n" + plugin_sections + "\n"


def _render_agents_md(
    project_name: str,
    profile: str,
    enabled_plugins: Iterable[str],
    production_plugins: Iterable[str],
) -> str:
    enabled_plugin_list = ", ".join(enabled_plugins) or "none"
    production_plugin_list = ", ".join(production_plugins) or "none"
    return f"""# AGENTS.md

Instructions for AI agents and automation working in this generated FastAPI project.

## Project Contract

- Project: `{project_name}`
- Scaffold profile: `{profile.strip() or "minimal"}`
- Enabled plugins: {enabled_plugin_list}
- Production plugins: {production_plugin_list}
- Use `infra.manifest.json` as the project contract.
- Keep generated files aligned with `infra.manifest.json`; run `fastapi-infra project-check .` after changing scaffold-owned files.

## Standard Commands

- Run `make env` before production checks.
- Run `make verify` before handing off changes.
- Run `make release-static` for static production readiness.
- Use `make provider-preflight` only after provider credentials are prepared.
- Use `make dev-up` for the generated Docker Compose stack.

## Environment Rules

- Keep runtime `.env` separate from provider `provider.env`.
- Runtime settings come from `.env` and `infra.production.example.toml`.
- Live provider certification credentials belong in `provider.env`.
- Do not commit `.env`, `provider.env`, or `provider-certification.json`.
- Do not run live provider certification unless explicitly requested.

## Change Rules

- Prefer existing app structure in `app/`, `tests/`, and generated plugin examples.
- Keep plugin enablement in `infra.toml` and `infra.production.example.toml` consistent with `infra.manifest.json`.
- When production plugins include `database`, keep `migrations/` present and included in release checks.
- Do not bypass `make verify` or `make release-static` with narrower commands when validating handoff readiness.
"""


def _render_project_manifest(
    project_name: str,
    profile: str,
    *,
    enabled_plugins: Iterable[str],
    requested_plugins: Iterable[str],
    production_plugins: Iterable[str],
    package_plugins: Iterable[str],
    manifest: Mapping[str, Mapping[str, object]],
) -> str:
    enabled = tuple(enabled_plugins)
    requested = _validate_plugins(requested_plugins, tuple(manifest))
    production = tuple(production_plugins)
    package = tuple(package_plugins)
    profile_plugin_tuple = plugins_for_profile(profile)
    plugin_entries = []
    builtin_names = set(_builtin_plugin_names())
    for plugin in package:
        plugin_manifest = manifest.get(plugin, {})
        plugin_entries.append(
            {
                "name": plugin,
                "built_in": plugin in builtin_names,
                "requested": plugin in enabled,
                "production_enabled": plugin in production,
                "services": _string_list(plugin_manifest.get("provides", [])),
                "env_vars": _string_list(plugin_manifest.get("env_vars", [])),
                "recommended_extras": _string_list(plugin_manifest.get("recommended_extras", [])),
                "production_dependencies": _string_list(
                    plugin_manifest.get("production_dependencies", [])
                ),
            }
        )
    payload = {
        "schema_version": 1,
        "generator": "fastapi-infra",
        "project_name": project_name,
        "profile": profile.strip() or "minimal",
        "profile_plugins": list(profile_plugin_tuple),
        "requested_plugins": list(requested),
        "enabled_plugins": list(enabled),
        "production_plugins": list(production),
        "package_plugins": list(package),
        "files": {
            "agent_instructions": "AGENTS.md",
            "ci_workflow": ".github/workflows/ci.yml",
            "compose": "compose.yaml",
            "dockerignore": ".dockerignore",
            "gitignore": ".gitignore",
            "makefile": "Makefile",
            "local_config": "infra.toml",
            "production_config": "infra.production.example.toml",
            "runtime_env_example": ".env.example",
            "provider_env_example": "provider.env.example",
            "prepare_env_script": "scripts/prepare-env.sh",
            "release_script": "scripts/verify-release.sh",
        },
        "commands": {
            "docker": ["make dev-up"],
            "prepare_env": ["make env"],
            "install": ['pip install -e ".[dev]"'],
            "local_verify": [
                "make verify",
            ],
            "production_static": [
                "make release-static",
            ],
            "provider_preflight": [
                "make provider-preflight",
            ],
            "release_script": ["scripts/verify-release.sh .env provider.env"],
        },
        "plugins": plugin_entries,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _render_project_ci_workflow() -> str:
    return """name: CI

on:
  push:
  pull_request:

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Prepare runtime env
        run: |
          make env
      - name: Local gates
        run: |
          make verify
      - name: Static production gates
        run: |
          make release-static
"""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _render_env_example(
    project_name: str,
    enabled_plugins: Iterable[str],
    *,
    manifest: Mapping[str, Mapping[str, object]] | None = None,
) -> str:
    lines = [
        f"APP_NAME={project_name}",
        "ENVIRONMENT=local",
    ]
    resolved_manifest = manifest or _plugin_manifest()
    seen_env_vars: set[str] = set()
    database_name = project_name.replace("-", "_")
    defaults = {**ENV_EXAMPLE_DEFAULTS, "MYSQL_DATABASE": database_name}

    for plugin in enabled_plugins:
        env_lines = []
        env_vars = resolved_manifest.get(plugin, {}).get("env_vars", [])
        if not isinstance(env_vars, list):
            continue
        for env_var in env_vars:
            if not isinstance(env_var, str) or env_var in seen_env_vars:
                continue
            env_lines.append(f"{env_var}={defaults.get(env_var, '')}")
            seen_env_vars.add(env_var)
        if env_lines:
            lines.extend(["", f"# {plugin} plugin", *env_lines])
    return "\n".join(lines) + "\n"


def _render_provider_env_example(
    production_config: Mapping[str, Any],
    runtime_env_example: str,
) -> str:
    from infra.provider_certification import format_provider_env_template, selected_checks
    from infra.release_check import expected_provider_check_names

    runtime_environ = _parse_env_example(runtime_env_example)
    data = _replace_env_references_for_scaffold(
        production_config,
        runtime_environ,
    )
    settings = InfraSettings(**data)
    provider_names = expected_provider_check_names(
        settings,
        plugins=_get_builtin_plugins_for_scaffold(),
    )
    if not provider_names:
        return (
            "# fastapi-infra live provider certification environment\n"
            "# No live provider checks are required by infra.production.example.toml.\n"
        )
    checks = selected_checks(list(provider_names))
    return format_provider_env_template(checks) + "\n"


def _provider_env_example_requires_credentials(content: str) -> bool:
    return "No live provider checks are required" not in content


def _get_builtin_plugins_for_scaffold():
    from infra.plugins.builtin import get_builtin_plugins

    return get_builtin_plugins()


def _parse_env_example(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _replace_env_references_for_scaffold(value: Any, environ: Mapping[str, str]) -> Any:
    if isinstance(value, list):
        return [_replace_env_references_for_scaffold(item, environ) for item in value]
    if isinstance(value, Mapping):
        if set(value) == {"$env"}:
            variable = value["$env"]
            if isinstance(variable, str):
                return environ.get(variable, _scaffold_env_placeholder(variable))
        return {
            key: _replace_env_references_for_scaffold(item, environ) for key, item in value.items()
        }
    return value


def _scaffold_env_placeholder(variable: str) -> str:
    if variable.endswith("_PORT"):
        return "1"
    if variable.endswith("_URL"):
        if "REDIS" in variable:
            return "redis://localhost:6379/0"
        return "https://example.test"
    if variable.endswith("_REGION"):
        return "us-east-1"
    if variable.endswith("_HOST"):
        return "localhost"
    if variable.endswith("_DATABASE") or variable.endswith("_DB"):
        return "app"
    if variable.endswith("_SENDER"):
        return "noreply@example.test"
    if variable.endswith("_RECIPIENT"):
        return "ops@example.test"
    if variable.endswith("_SECRET") or variable.endswith("_PASSWORD"):
        return "placeholder-secret-value"
    if variable.endswith("_API_KEY"):
        return "placeholder-api-key"
    return "placeholder"


def _render_infra_toml(
    enabled_plugins: Iterable[str],
    manifest_config_key: str = "local_config_example",
    *,
    env_references: bool = False,
    config_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    manifest: Mapping[str, Mapping[str, object]] | None = None,
    plugin_names: Iterable[str] | None = None,
) -> str:
    enabled = set(enabled_plugins)
    resolved_manifest = manifest or _plugin_manifest()
    names = tuple(plugin_names or _builtin_plugin_names())
    overrides = config_overrides or {}
    lines: list[str] = []
    for plugin in names:
        lines.extend(
            [
                f"[infra.plugins.{plugin}]",
                f"enabled = {str(plugin in enabled).lower()}",
                "",
            ]
        )
        if plugin in enabled:
            config = resolved_manifest.get(plugin, {}).get(manifest_config_key, {})
            if isinstance(config, Mapping) and config:
                config = _deep_merge_mapping(config, overrides.get(plugin, {}))
                lines.extend(
                    _render_toml_mapping(
                        f"infra.plugins.{plugin}.config",
                        config,
                        env_references=env_references,
                    )
                )
                lines.append("")
    return "\n".join(lines)


def _migration_files_for_plugins(
    enabled_plugins: Iterable[str],
    *,
    manifest: Mapping[str, Mapping[str, object]] | None = None,
    plugin_names: Iterable[str] | None = None,
) -> dict[Path, str]:
    enabled = set(enabled_plugins)
    resolved_manifest = manifest or _plugin_manifest()
    names = tuple(plugin_names or _builtin_plugin_names())
    files: dict[Path, str] = {}
    for plugin in names:
        if plugin not in enabled:
            continue
        migrations = resolved_manifest.get(plugin, {}).get("migrations", [])
        if not isinstance(migrations, list):
            continue
        for migration in migrations:
            if not isinstance(migration, Mapping):
                continue
            version = migration.get("version")
            name = migration.get("name")
            sql = migration.get("sql")
            if (
                not isinstance(version, str)
                or not isinstance(name, str)
                or not isinstance(sql, str)
            ):
                continue
            files[Path("migrations") / f"{version}_{name}.sql"] = sql.rstrip() + "\n"
    return files


def _scaffold_files_for_plugins(
    enabled_plugins: Iterable[str],
    *,
    manifest: Mapping[str, Mapping[str, object]],
) -> tuple[dict[Path, str], set[Path]]:
    files: dict[Path, str] = {}
    executable_paths: set[Path] = set()
    for plugin in enabled_plugins:
        scaffold_files = manifest.get(plugin, {}).get("scaffold_files", [])
        if not isinstance(scaffold_files, list):
            continue
        for scaffold_file in scaffold_files:
            if not isinstance(scaffold_file, Mapping):
                continue
            path_value = scaffold_file.get("path")
            content = scaffold_file.get("content")
            executable = scaffold_file.get("executable", False)
            if not isinstance(path_value, str) or not isinstance(content, str):
                continue
            path = Path(path_value)
            if path in files:
                raise ValueError(f"duplicate plugin scaffold file: {path}")
            files[path] = content
            if executable is True:
                executable_paths.add(path)
    return files, executable_paths


def _scaffold_readme_sections_for_plugins(
    enabled_plugins: Iterable[str],
    *,
    manifest: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    sections: list[str] = []
    for plugin in enabled_plugins:
        plugin_sections = manifest.get(plugin, {}).get("scaffold_readme_sections", [])
        if not isinstance(plugin_sections, list):
            continue
        sections.extend(section for section in plugin_sections if isinstance(section, str))
    return tuple(sections)


def _app_migration_files_for_plugins(enabled_plugins: Iterable[str]) -> dict[Path, str]:
    if "database" not in set(enabled_plugins):
        return {}
    return {
        Path(
            "migrations/00000000000100_app_documents.sql"
        ): """CREATE TABLE IF NOT EXISTS infra_documents (
    collection VARCHAR(128) NOT NULL,
    document_key VARCHAR(255) NOT NULL,
    document_value JSON NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (collection, document_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
""",
    }


def _production_plugins_for(
    enabled_plugins: Iterable[str],
    *,
    manifest: Mapping[str, Mapping[str, object]] | None = None,
    plugin_names: Iterable[str] | None = None,
) -> tuple[str, ...]:
    names = tuple(plugin_names or _builtin_plugin_names())
    requested = _validate_plugins(enabled_plugins, names)
    resolved_manifest = manifest or _plugin_manifest()
    production_plugins = set(requested)
    changed = True
    while changed:
        changed = False
        for plugin in tuple(production_plugins):
            dependencies = resolved_manifest.get(plugin, {}).get("production_dependencies", [])
            if not isinstance(dependencies, list):
                continue
            for dependency in dependencies:
                if not isinstance(dependency, str):
                    continue
                if dependency not in production_plugins:
                    production_plugins.add(dependency)
                    changed = True
    return tuple(plugin for plugin in names if plugin in production_plugins)


def _production_config_overrides(enabled_plugins: Iterable[str]) -> dict[str, Mapping[str, Any]]:
    requested = set(enabled_plugins)
    if "database" in requested:
        return {}
    if not ({"payment", "tasks", "ratelimit"} & requested):
        return {}

    database_config: dict[str, Any] = {
        "mysql_enabled": "payment" in requested,
        "redis_enabled": bool({"tasks", "ratelimit"} & requested),
    }
    if "payment" not in requested:
        database_config.update(
            {
                "mysql_host": None,
                "mysql_port": None,
                "mysql_user": None,
                "mysql_password": None,
                "mysql_db": None,
            }
        )
    if not ({"tasks", "ratelimit"} & requested):
        database_config["redis_url"] = None

    return {"database": {"config": database_config}}


def _deep_merge_mapping(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge_mapping(existing, value)
        else:
            merged[key] = value
    return merged


def _render_toml_mapping(
    section: str,
    values: Mapping[str, Any],
    *,
    env_references: bool = False,
) -> list[str]:
    lines = [f"[{section}]"]
    nested: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in values.items():
        if value is None:
            continue
        env_var = _env_placeholder(value) if env_references else None
        if env_var is not None:
            lines.append(f'{key} = {{ "$env" = {_toml_scalar(env_var)} }}')
            continue
        if isinstance(value, Mapping):
            nested.append((str(key), value))
            continue
        lines.append(f"{key} = {_toml_scalar(value)}")

    for key, value in nested:
        lines.append("")
        lines.extend(
            _render_toml_mapping(
                f"{section}.{key}",
                value,
                env_references=env_references,
            )
        )
    return lines


def _env_placeholder(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", value)
    if match is None:
        return None
    return match.group(1)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML scaffold value: {value!r}")


def _validate_plugins(
    enabled_plugins: Iterable[str],
    plugin_names: Iterable[str] | None = None,
) -> tuple[str, ...]:
    plugins = tuple(enabled_plugins)
    available = tuple(plugin_names or _builtin_plugin_names())
    unknown = sorted(set(plugins) - set(available))
    if unknown:
        raise ValueError(
            "unknown plugin name: "
            + ", ".join(unknown)
            + ". available plugins: "
            + ", ".join(available)
            + ". Run fastapi-infra plugins to list plugin metadata."
        )
    invalid = [plugin for plugin in plugins if not PLUGIN_NAME_RE.fullmatch(plugin)]
    if invalid:
        raise ValueError(f"invalid plugin name: {', '.join(invalid)}")
    unique_plugins: list[str] = []
    seen: set[str] = set()
    for plugin in plugins:
        if plugin in seen:
            continue
        unique_plugins.append(plugin)
        seen.add(plugin)
    return tuple(unique_plugins)


def _extras_for_plugins(
    enabled_plugins: Iterable[str],
    *,
    manifest: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[str, ...]:
    extras: set[str] = set()
    resolved_manifest = manifest or _plugin_manifest()
    for plugin in enabled_plugins:
        recommended = resolved_manifest.get(plugin, {}).get("recommended_extras", [])
        if isinstance(recommended, list):
            extras.update(extra for extra in recommended if isinstance(extra, str))
    return tuple(sorted(extras))
