import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from infra.config import load_infra_settings, validate_infra_settings
from infra.core.health import HealthState
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.manager import PluginManager
from infra.release_check import build_release_check_report
from infra.scaffold import (
    create_project,
    merge_profile_plugins,
    plugin_profiles,
    plugins_for_profile,
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def env_names_from_example(content: str) -> set[str]:
    names = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        names.add(stripped.split("=", 1)[0])
    return names


def load_generated_app(root: Path) -> Any:
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module("app.main").app
    finally:
        sys.path.remove(str(root))


class ExternalScaffoldPlugin:
    metadata = PluginMetadata(
        name="search",
        version="1.0.0",
        provides=["search"],
    )
    config_model = None
    manifest_hints = {
        "recommended_extras": ["http"],
        "env_vars": ["SEARCH_ENDPOINT", "SEARCH_API_KEY"],
        "local_config_example": {"endpoint": "mock://search"},
        "production_config_example": {
            "endpoint": "${SEARCH_ENDPOINT}",
            "api_key": "${SEARCH_API_KEY}",
        },
        "scaffold_files": [
            {
                "path": "app/search.py",
                "content": "def search_status() -> str:\n    return 'ready'\n",
            },
            {
                "path": "scripts/search-sync.sh",
                "content": "#!/usr/bin/env sh\necho search-sync\n",
                "executable": True,
            },
        ],
        "scaffold_readme_sections": [
            "## Search\n\nThe external search plugin added this section.\n"
        ],
    }

    def register(self, ctx: PluginContext) -> None:
        ctx.services["search"] = object()

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("search", HealthState.HEALTHY)


def test_create_project_writes_expected_files_and_imports(tmp_path):
    root = tmp_path / "service"
    created = create_project(root, "billing_api")

    _assert_minimal_project_file_set(root, created)
    _assert_minimal_app_main(root)
    _assert_minimal_project_config_files(root)
    _assert_minimal_project_runtime_files(root)
    _assert_minimal_project_scripts(root)
    _assert_minimal_project_docs(root)
    _assert_minimal_project_health_tests(root)
    _assert_minimal_project_manifest(root)


def _assert_minimal_project_file_set(root: Path, created: list[Path]) -> None:
    created_relative = {path.relative_to(root) for path in created}
    assert created_relative == {
        Path("AGENTS.md"),
        Path(".github/workflows/ci.yml"),
        Path(".dockerignore"),
        Path(".gitignore"),
        Path(".env.example"),
        Path("Dockerfile"),
        Path("Makefile"),
        Path("README.md"),
        Path("app/main.py"),
        Path("app/settings.py"),
        Path("compose.yaml"),
        Path("infra.manifest.json"),
        Path("infra.production.example.toml"),
        Path("infra.toml"),
        Path("provider.env.example"),
        Path("pyproject.toml"),
        Path("scripts/prepare-env.sh"),
        Path("scripts/verify-release.sh"),
        Path("tests/test_config.py"),
        Path("tests/test_health.py"),
    }


def _assert_minimal_app_main(root: Path) -> None:
    main_py = read(root / "app" / "main.py")
    assert "from fastapi import FastAPI" in main_py
    assert "from infra import InfraSettings, setup_infra" in main_py
    assert "from infra.middleware import (" in main_py
    assert "ErrorHandlingMiddleware" in main_py
    assert "RequestLoggingMiddleware" in main_py
    assert "SecurityHeadersMiddleware" in main_py
    assert "install_error_handlers" in main_py
    assert 'app = FastAPI(title="billing_api")' in main_py
    assert "install_error_handlers(app)" in main_py
    assert "app.add_middleware(ErrorHandlingMiddleware)" in main_py
    assert "app.add_middleware(RequestLoggingMiddleware)" in main_py
    assert "app.add_middleware(SecurityHeadersMiddleware)" in main_py
    assert "infra.plugins.observability" not in main_py


def _assert_minimal_project_config_files(root: Path) -> None:
    settings_py = read(root / "app" / "settings.py")
    assert "def build_settings() -> InfraSettings:" in settings_py
    assert "INFRA_SETTINGS" in settings_py
    assert "load_infra_settings(config_path)" in settings_py
    pyproject = read(root / "pyproject.toml")
    assert "[project.optional-dependencies]" in pyproject
    assert '"pytest>=8.0.0"' in pyproject
    assert '"httpx>=0.27.0,<0.29.0"' in pyproject
    config_test = read(root / "tests" / "test_config.py")
    assert "def test_local_infra_config_loads_and_validates" in config_test
    assert "def test_local_infra_config_passes_cli_config_check" in config_test
    assert "def test_production_config_example_passes_cli_config_check" in config_test
    assert 'str(ROOT / ".env.example")' in config_test
    assert '"-m",\n            "infra.cli"' in config_test
    assert "validate_infra_settings(settings, get_available_plugins(settings)) == []" in config_test


def _assert_minimal_project_runtime_files(root: Path) -> None:
    dockerfile = read(root / "Dockerfile")
    assert dockerfile.startswith("FROM python:3.11-slim")
    assert "ENV INFRA_SETTINGS=infra.toml" in dockerfile
    assert (
        "COPY pyproject.toml README.md infra.toml infra.production.example.toml "
        "infra.manifest.json ./" in dockerfile
    )
    assert "COPY scripts ./scripts" in dockerfile
    assert "COPY migrations ./migrations" not in dockerfile
    assert "chmod +x scripts/*.sh" in dockerfile
    assert 'adduser --disabled-password --gecos "" appuser' in dockerfile
    assert "USER appuser" in dockerfile
    assert "HEALTHCHECK --interval=30s" in dockerfile
    assert "urllib.request.urlopen('http://127.0.0.1:8000/health'" in dockerfile
    makefile = read(root / "Makefile")
    assert "install:" in makefile
    assert 'pip install -e ".[dev]"' in makefile
    assert "run:" in makefile
    assert "uvicorn app.main:app --reload" in makefile
    assert "verify: config-check project-check test" in makefile
    assert "env:" in makefile
    assert "scripts/prepare-env.sh $(RUNTIME_ENV_FILE) $(PROVIDER_ENV_FILE)" in makefile
    assert "release-static:" in makefile
    assert (
        "fastapi-infra release-check --settings infra.production.example.toml "
        "--env-file $(RUNTIME_ENV_FILE) --static-only"
    ) in makefile
    assert "--migrations migrations --static-only" not in makefile
    assert "provider-preflight:" in makefile
    assert "dev-up:" in makefile
    assert "docker compose up --build" in makefile
    ci_workflow = read(root / ".github/workflows/ci.yml")
    assert 'pip install -e ".[dev]"' in ci_workflow
    assert "make env" in ci_workflow
    assert "make verify" in ci_workflow
    assert "make release-static" in ci_workflow
    assert "scripts/prepare-env.sh .env provider.env" not in ci_workflow
    assert "fastapi-infra config-check --settings infra.toml" not in ci_workflow
    assert "fastapi-infra project-check ." not in ci_workflow
    assert "python -m pytest -q" not in ci_workflow
    assert "fastapi-infra release-check --settings infra.production.example.toml" not in ci_workflow
    assert "--migrations migrations --static-only" not in ci_workflow
    dockerignore = read(root / ".dockerignore")
    assert ".env" in dockerignore
    assert "provider.env" in dockerignore
    assert "provider-env-template.env" in dockerignore
    assert "provider-certification.json" in dockerignore
    assert "provider-preflight.json" in dockerignore
    gitignore = read(root / ".gitignore")
    assert ".env" in gitignore
    assert "provider.env" in gitignore
    assert "provider-env-template.env" in gitignore
    assert "provider-certification.json" in gitignore
    assert "provider-preflight.json" in gitignore
    assert ".coverage" in gitignore
    assert "htmlcov" in gitignore
    compose = read(root / "compose.yaml")
    assert "services:" in compose
    assert "  app:" in compose
    assert "    build: ." in compose
    assert "      - .env" in compose
    assert "      INFRA_SETTINGS: infra.production.example.toml" in compose
    assert '      - "8000:8000"' in compose
    assert "  mysql:" not in compose
    assert "  redis:" not in compose


def _assert_minimal_project_scripts(root: Path) -> None:
    verify_script = read(root / "scripts" / "verify-release.sh")
    assert verify_script.startswith("#!/usr/bin/env sh")
    assert "make verify" in verify_script
    assert 'RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" make release-static' in verify_script
    assert 'RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" make provider-list' in verify_script
    assert (
        'RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" PROVIDER_ENV_FILE="$PROVIDER_ENV_FILE" '
        "make provider-preflight"
    ) in verify_script
    assert (
        "fastapi-infra certify-providers --settings infra.production.example.toml "
        '--settings-env-file "$RUNTIME_ENV_FILE" --preflight' in verify_script
    )
    assert (
        "fastapi-infra certify-providers --settings infra.production.example.toml "
        '--settings-env-file "$RUNTIME_ENV_FILE" --preflight --env-file "$PROVIDER_ENV_FILE"'
        not in verify_script
    )
    assert "unsafe JWT_SECRET in $RUNTIME_ENV_FILE" not in verify_script
    assert "RUN_LIVE_CERTIFICATION" in verify_script
    assert "copy provider.env.example to provider.env" in verify_script
    assert "--migrations migrations --static-only" not in verify_script
    prepare_env_script = read(root / "scripts" / "prepare-env.sh")
    assert prepare_env_script.startswith("#!/usr/bin/env sh")
    assert "secrets.token_urlsafe(32)" in prepare_env_script
    assert "JWT_SECRET" in prepare_env_script
    assert 'cp .env.example "$RUNTIME_ENV_FILE"' in prepare_env_script
    assert 'cp provider.env.example "$PROVIDER_ENV_FILE"' in prepare_env_script
    assert (root / "scripts" / "verify-release.sh").stat().st_mode & 0o111
    assert (root / "scripts" / "prepare-env.sh").stat().st_mode & 0o111
    provider_env_example = read(root / "provider.env.example")
    assert "No live provider checks are required" in provider_env_example


def _assert_minimal_project_docs(root: Path) -> None:
    generated_readme = read(root / "README.md")
    assert "for auth profiles it also replaces the unsafe example `JWT_SECRET`" in generated_readme
    assert "make verify" in generated_readme
    assert "make env" in generated_readme
    assert "make release-static" in generated_readme
    assert "make dev-up" in generated_readme
    assert "fastapi-infra project-check ." in generated_readme
    assert "fastapi-infra project-check . --json" in generated_readme
    assert "cp .env.example .env" not in generated_readme
    assert "cp provider.env.example provider.env" not in generated_readme
    assert "scripts/verify-release.sh .env provider.env" in generated_readme
    assert "docker compose up --build" in generated_readme
    assert (
        "fastapi-infra release-check --settings infra.production.example.toml "
        "--env-file .env --static-only" in generated_readme
    )
    assert "--migrations migrations --static-only" not in generated_readme
    agents_md = read(root / "AGENTS.md")
    assert agents_md.startswith("# AGENTS.md")
    assert "Project: `billing_api`" in agents_md
    assert "Scaffold profile: `minimal`" in agents_md
    assert "Enabled plugins: none" in agents_md
    assert "Production plugins: none" in agents_md
    assert "Use `infra.manifest.json` as the project contract." in agents_md
    assert "Run `make env` before production checks." in agents_md
    assert "Run `make verify` before handing off changes." in agents_md
    assert "Run `make release-static` for static production readiness." in agents_md
    assert "Keep runtime `.env` separate from provider `provider.env`." in agents_md
    assert "Do not run live provider certification unless explicitly requested." in agents_md


def _assert_minimal_project_health_tests(root: Path) -> None:
    health_test = read(root / "tests" / "test_health.py")
    assert "def test_health_returns_snapshot" in health_test
    assert "def test_health_includes_trace_headers" in health_test
    assert "def test_health_includes_security_headers" in health_test
    assert "def test_enabled_plugin_services_are_registered" in health_test
    assert "EXPECTED_SERVICES = []" in health_test


def _assert_minimal_project_manifest(root: Path) -> None:
    project_manifest = json.loads(read(root / "infra.manifest.json"))
    assert project_manifest["schema_version"] == 1
    assert project_manifest["generator"] == "fastapi-infra"
    assert project_manifest["project_name"] == "billing_api"
    assert project_manifest["profile"] == "minimal"
    assert project_manifest["enabled_plugins"] == []
    assert project_manifest["production_plugins"] == []
    assert project_manifest["files"]["agent_instructions"] == "AGENTS.md"
    assert project_manifest["files"]["compose"] == "compose.yaml"
    assert project_manifest["files"]["ci_workflow"] == ".github/workflows/ci.yml"
    assert project_manifest["files"]["dockerignore"] == ".dockerignore"
    assert project_manifest["files"]["gitignore"] == ".gitignore"
    assert project_manifest["files"]["makefile"] == "Makefile"
    assert project_manifest["files"]["prepare_env_script"] == "scripts/prepare-env.sh"
    assert project_manifest["files"]["release_script"] == "scripts/verify-release.sh"
    assert project_manifest["commands"]["docker"] == ["make dev-up"]
    assert project_manifest["commands"]["prepare_env"] == ["make env"]
    assert project_manifest["commands"]["install"] == ['pip install -e ".[dev]"']
    assert project_manifest["commands"]["local_verify"] == ["make verify"]
    assert project_manifest["commands"]["production_static"] == ["make release-static"]
    assert project_manifest["commands"]["provider_preflight"] == ["make provider-preflight"]
    assert project_manifest["commands"]["release_script"] == [
        "scripts/verify-release.sh .env provider.env"
    ]


def test_create_project_configures_only_requested_plugins(tmp_path):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("auth", "tasks"))

    _assert_auth_tasks_local_config(root)
    _assert_auth_tasks_main_py(root)
    _assert_auth_tasks_release_files(root)
    _assert_auth_tasks_runtime_files(root)
    _assert_auth_tasks_manifest(root)
    _assert_auth_tasks_health_tests(root)


def _assert_auth_tasks_local_config(root: Path) -> None:
    infra_toml = read(root / "infra.toml")
    assert "[infra.plugins.auth]\nenabled = true" in infra_toml
    assert "[infra.plugins.auth.config]\njwt_secret = " in infra_toml
    assert "[infra.plugins.tasks]\nenabled = true" in infra_toml
    assert '[infra.plugins.tasks.config]\ndefault_provider = "memory"' in infra_toml
    assert "[infra.plugins.ai]\nenabled = false" in infra_toml
    assert "[infra.plugins.observability]\nenabled = false" in infra_toml
    assert "[infra.plugins.payment]\nenabled = false" in infra_toml


def _assert_auth_tasks_main_py(root: Path) -> None:
    main_py = read(root / "app" / "main.py")
    assert "from fastapi import Depends, FastAPI" in main_py
    assert "from infra.plugins import TASKS_SERVICE" in main_py
    assert "STORAGE_SERVICE" not in main_py
    assert "from infra.plugins.auth import Principal, require_principal" in main_py
    assert '@app.get("/me")' in main_py
    assert "Depends(require_principal)" in main_py
    assert '@app.post("/storage/example")' not in main_py
    assert '@app.post("/tasks/example")' in main_py
    assert 'queue.enqueue("example.ping", {"source": "api"})' in main_py


def _assert_auth_tasks_release_files(root: Path) -> None:
    generated_readme = read(root / "README.md")
    assert 'pip install -e ".[dev]"' in generated_readme
    assert "scripts/verify-release.sh .env provider.env" in generated_readme
    assert "fastapi-infra config-check --settings infra.toml" in generated_readme
    assert (
        "fastapi-infra config-check --settings infra.production.example.toml "
        "--env-file .env" in generated_readme
    )
    assert (
        "fastapi-infra release-check --settings infra.production.example.toml "
        "--env-file .env --migrations migrations --static-only" in generated_readme
    )
    assert (
        "fastapi-infra certify-providers --settings infra.production.example.toml "
        "--settings-env-file .env --list --requirements" in generated_readme
    )
    assert (
        "fastapi-infra certify-providers --settings infra.production.example.toml "
        "--settings-env-file .env --preflight --env-file provider.env" in generated_readme
    )
    assert "docker build -t billing_api ." in generated_readme
    assert "INFRA_SETTINGS=infra.production.example.toml" in generated_readme
    assert "python -m pytest -q" in generated_readme
    verify_script = read(root / "scripts" / "verify-release.sh")
    assert "unsafe JWT_SECRET in $RUNTIME_ENV_FILE" in verify_script
    assert "replace it with a random secret of at least 32 characters" in verify_script
    assert 'RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" make release-static' in verify_script
    assert (
        'RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" PROVIDER_ENV_FILE="$PROVIDER_ENV_FILE" '
        "make provider-preflight" in verify_script
    )
    assert (
        "fastapi-infra certify-providers --settings infra.production.example.toml "
        '--settings-env-file "$RUNTIME_ENV_FILE" --env-file "$PROVIDER_ENV_FILE" --json '
        "> provider-certification.json" in verify_script
    )
    ci_workflow = read(root / ".github/workflows/ci.yml")
    assert "make env" in ci_workflow
    assert "make verify" in ci_workflow
    assert "make release-static" in ci_workflow
    assert "fastapi-infra release-check --settings infra.production.example.toml" not in ci_workflow


def _assert_auth_tasks_runtime_files(root: Path) -> None:
    compose = read(root / "compose.yaml")
    assert "      REDIS_URL: redis://redis:6379/0" in compose
    assert "  redis:" in compose
    assert '      test: ["CMD", "redis-cli", "ping"]' in compose
    assert "  redis_data:" in compose
    assert "  mysql:" not in compose
    dockerfile = read(root / "Dockerfile")
    assert "COPY migrations ./migrations" in dockerfile
    makefile = read(root / "Makefile")
    assert "--migrations migrations --static-only" in makefile
    provider_env_example = read(root / "provider.env.example")
    assert "REDIS_LIVE_URL=" in provider_env_example
    assert "MYSQL_LIVE_HOST" not in provider_env_example


def _assert_auth_tasks_manifest(root: Path) -> None:
    project_manifest = json.loads(read(root / "infra.manifest.json"))
    assert project_manifest["enabled_plugins"] == ["auth", "tasks"]
    assert project_manifest["requested_plugins"] == ["auth", "tasks"]
    assert project_manifest["production_plugins"] == ["auth", "database", "tasks"]
    assert project_manifest["package_plugins"] == ["auth", "tasks", "database"]
    assert [plugin["name"] for plugin in project_manifest["plugins"]] == [
        "auth",
        "tasks",
        "database",
    ]
    assert [plugin["requested"] for plugin in project_manifest["plugins"]] == [
        True,
        True,
        False,
    ]
    assert [plugin["production_enabled"] for plugin in project_manifest["plugins"]] == [
        True,
        True,
        True,
    ]
    assert project_manifest["commands"]["production_static"] == ["make release-static"]


def _assert_auth_tasks_health_tests(root: Path) -> None:
    health_test = read(root / "tests" / "test_health.py")
    assert "from infra.plugins import AUTH_SERVICE, TASKS_SERVICE" in health_test
    assert "EXPECTED_SERVICES = ['auth', 'tasks']" in health_test
    assert "assert infra.get(service_name) is not None" in health_test
    assert "def test_auth_me_requires_bearer_token" in health_test
    assert "def test_auth_me_accepts_issued_jwt" in health_test
    assert "def test_tasks_example_route_enqueues_task" in health_test


def test_prepare_env_script_generates_safe_jwt_secret(tmp_path):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("auth",))

    result = subprocess.run(
        ["scripts/prepare-env.sh", ".env", "provider.env"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    runtime_env = read(root / ".env")
    assert "JWT_SECRET=replace-with-32-byte-random-secret" not in runtime_env
    jwt_secret = next(line for line in runtime_env.splitlines() if line.startswith("JWT_SECRET="))
    assert len(jwt_secret.removeprefix("JWT_SECRET=")) >= 32
    assert read(root / "provider.env") == read(root / "provider.env.example")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "infra.cli",
            "release-check",
            "--settings",
            "infra.production.example.toml",
            "--env-file",
            ".env",
            "--migrations",
            "migrations",
            "--static-only",
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "release-check: ready" in result.stdout


def test_create_project_deduplicates_requested_plugins(tmp_path):
    root = tmp_path / "service"

    create_project(root, "billing_api", enabled_plugins=("auth", "auth", "tasks", "auth"))

    assert "Enabled plugins: auth, tasks" in read(root / "README.md")
    assert "auth, auth" not in read(root / "README.md")
    assert "EXPECTED_SERVICES = ['auth', 'tasks']" in read(root / "tests" / "test_health.py")


def test_create_project_preserves_requested_plugin_iterator_in_manifest(tmp_path):
    root = tmp_path / "service"
    requested_plugins = (plugin for plugin in ("auth", "tasks"))

    create_project(root, "billing_api", enabled_plugins=requested_plugins)

    project_manifest = json.loads(read(root / "infra.manifest.json"))
    assert project_manifest["requested_plugins"] == ["auth", "tasks"]
    assert project_manifest["enabled_plugins"] == ["auth", "tasks"]


def test_create_project_can_use_profile_and_extra_plugins(tmp_path):
    root = tmp_path / "service"

    create_project(root, "billing_api", profile="api", enabled_plugins=("tasks", "auth"))

    infra_toml = read(root / "infra.toml")
    assert "[infra.plugins.auth]\nenabled = true" in infra_toml
    assert "[infra.plugins.database]\nenabled = true" in infra_toml
    assert "[infra.plugins.cache]\nenabled = true" in infra_toml
    assert "[infra.plugins.http]\nenabled = true" in infra_toml
    assert "[infra.plugins.observability]\nenabled = true" in infra_toml
    assert "[infra.plugins.ratelimit]\nenabled = true" in infra_toml
    assert "[infra.plugins.tasks]\nenabled = true" in infra_toml
    assert "[infra.plugins.payment]\nenabled = false" in infra_toml
    assert "Enabled plugins: auth, database, cache, http, observability, ratelimit, tasks" in read(
        root / "README.md"
    )


def test_create_project_can_apply_external_plugin_scaffold_hints(tmp_path):
    root = tmp_path / "service"
    plugin_registry = [*get_builtin_plugins(), ExternalScaffoldPlugin()]

    created = create_project(
        root,
        "billing_api",
        enabled_plugins=("search",),
        plugin_registry=plugin_registry,
    )

    assert root.joinpath("app/search.py").exists()
    assert read(root / "app/search.py") == "def search_status() -> str:\n    return 'ready'\n"
    assert root.joinpath("scripts/search-sync.sh").stat().st_mode & 0o111
    assert root / "app/search.py" in created
    assert root / "scripts/search-sync.sh" in created
    assert "[infra.plugins.search]\nenabled = true" in read(root / "infra.toml")
    assert 'endpoint = "mock://search"' in read(root / "infra.toml")
    assert "[infra.plugins.ai]\nenabled = false" in read(root / "infra.toml")
    assert "SEARCH_ENDPOINT=" in read(root / ".env.example")
    assert "SEARCH_API_KEY=" in read(root / ".env.example")
    assert '"fastapi-infra[http]"' in read(root / "pyproject.toml")
    assert "Enabled plugins: search" in read(root / "README.md")
    assert "The external search plugin added this section." in read(root / "README.md")
    assert "EXPECTED_SERVICES = ['search']" in read(root / "tests/test_health.py")
    assert "from infra.plugins.discovery import get_available_plugins" in read(
        root / "tests/test_config.py"
    )
    project_manifest = json.loads(read(root / "infra.manifest.json"))
    assert project_manifest["enabled_plugins"] == ["search"]
    assert project_manifest["plugins"][-1]["name"] == "search"
    assert project_manifest["plugins"][-1]["built_in"] is False
    assert project_manifest["plugins"][-1]["services"] == ["search"]


def test_plugin_profiles_reference_valid_builtin_plugins():
    builtin_names = {
        plugin.metadata.name
        for plugin in get_builtin_plugins()
        if isinstance(plugin.metadata.name, str)
    }

    profiles = plugin_profiles()

    assert plugins_for_profile("minimal") == ()
    assert plugins_for_profile("api") == (
        "auth",
        "database",
        "cache",
        "http",
        "observability",
        "ratelimit",
    )
    assert "ai" in plugins_for_profile("full")
    assert "payment" in plugins_for_profile("full")
    for profile_plugins in profiles.values():
        assert set(profile_plugins).issubset(builtin_names)


def test_merge_profile_plugins_preserves_order_and_deduplicates_extras():
    assert merge_profile_plugins("api", ("tasks", "auth")) == (
        "auth",
        "database",
        "cache",
        "http",
        "observability",
        "ratelimit",
        "tasks",
    )


def test_create_project_local_plugin_config_passes_static_validation(tmp_path):
    root = tmp_path / "service"
    create_project(
        root,
        "billing_api",
        enabled_plugins=(
            "ai",
            "speech",
            "auth",
            "database",
            "cache",
            "http",
            "observability",
            "tasks",
            "storage",
            "webhooks",
            "payment",
            "ratelimit",
            "notifications",
        ),
    )

    settings = load_infra_settings(root / "infra.toml")

    assert validate_infra_settings(settings, get_builtin_plugins()) == []


def test_create_project_writes_production_config_example(tmp_path, monkeypatch):
    root = tmp_path / "service"
    create_project(
        root,
        "billing_api",
        enabled_plugins=(
            "ai",
            "speech",
            "auth",
            "database",
            "cache",
            "http",
            "observability",
            "tasks",
            "storage",
            "webhooks",
            "payment",
            "notifications",
        ),
    )

    production_toml = read(root / "infra.production.example.toml")
    assert "[infra.plugins.ai]\nenabled = true" in production_toml
    assert 'api_key = { "$env" = "OPENAI_API_KEY" }' in production_toml
    assert "[infra.plugins.storage.config.providers.s3]" in production_toml
    assert 'webhook_secret = { "$env" = "STRIPE_WEBHOOK_SECRET" }' in production_toml
    assert "[infra.plugins.ratelimit]\nenabled = false" in production_toml

    env_values = {
        "OPENAI_API_KEY": "sk-openai",
        "JWT_SECRET": "jwt-secret",
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "root",
        "MYSQL_PASSWORD": "mysql-password",
        "MYSQL_DATABASE": "billing",
        "REDIS_URL": "redis://localhost:6379/0",
        "S3_LIVE_BUCKET": "bucket",
        "S3_LIVE_REGION": "us-east-1",
        "S3_LIVE_ACCESS_KEY_ID": "access",
        "S3_LIVE_SECRET_ACCESS_KEY": "secret",
        "S3_LIVE_ENDPOINT_URL": "https://s3.example.test",
        "STRIPE_API_KEY": "sk-stripe",
        "STRIPE_WEBHOOK_SECRET": "whsec_test",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_SENDER": "noreply@example.com",
        "SMTP_USERNAME": "smtp-user",
        "SMTP_PASSWORD": "smtp-password",
    }
    for name, value in env_values.items():
        monkeypatch.setenv(name, value)

    settings = load_infra_settings(root / "infra.production.example.toml")

    assert validate_infra_settings(settings, get_builtin_plugins()) == []


def test_create_project_payment_production_profile_adds_mysql_database(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("payment",))

    local_toml = read(root / "infra.toml")
    production_toml = read(root / "infra.production.example.toml")

    assert "[infra.plugins.payment]\nenabled = true" in local_toml
    assert "[infra.plugins.database]\nenabled = false" in local_toml
    assert "[infra.plugins.payment]\nenabled = true" in production_toml
    assert "[infra.plugins.database]\nenabled = true" in production_toml
    assert "mysql_enabled = true" in production_toml
    assert "redis_enabled = false" in production_toml
    assert "redis_url" not in production_toml
    assert "Production profile plugins: database, webhooks, payment" in read(root / "README.md")
    compose = read(root / "compose.yaml")
    assert "      MYSQL_HOST: mysql" in compose
    assert "  mysql:" in compose
    assert "      MYSQL_ROOT_PASSWORD: ${MYSQL_PASSWORD:-local-password}" in compose
    assert '      test: ["CMD-SHELL", "mysqladmin ping' in compose
    assert "  mysql_data:" in compose
    assert "  redis:" not in compose
    payment_migration = root / "migrations/00000000001000_infra_payment_store.sql"
    webhook_migration = root / "migrations/00000000001100_infra_webhook_store.sql"
    assert payment_migration.exists()
    assert webhook_migration.exists()
    assert "CREATE TABLE IF NOT EXISTS infra_payment_checkouts" in read(payment_migration)
    assert "CREATE TABLE IF NOT EXISTS infra_payment_refunds" in read(payment_migration)
    assert "CREATE TABLE IF NOT EXISTS infra_webhook_events" in read(webhook_migration)
    main_py = read(root / "app" / "main.py")
    assert "from infra.plugins.webhooks import SqlWebhookStore, install_webhook_routes" in main_py
    assert "from contextlib import asynccontextmanager" in main_py
    assert "async def lifespan(app: FastAPI)" in main_py
    assert 'app = FastAPI(title="billing_api", lifespan=lifespan)' in main_py
    assert "webhooks = infra.get(WEBHOOKS_SERVICE)" in main_py
    assert "install_webhook_routes(app, webhooks, store=store)" in main_py
    assert "app.state.webhook_routes_installed = True" in main_py

    for name, value in {
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "root",
        "MYSQL_PASSWORD": "mysql-password",
        "MYSQL_DATABASE": "billing",
        "STRIPE_API_KEY": "sk-stripe",
        "STRIPE_WEBHOOK_SECRET": "whsec_test",
    }.items():
        monkeypatch.setenv(name, value)

    settings = load_infra_settings(root / "infra.production.example.toml")
    report = build_release_check_report(settings, require_provider_certification=False)

    assert report["ready"] is True


def test_create_project_webhooks_includes_schema_migration(tmp_path):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("webhooks",))

    webhook_migration = root / "migrations/00000000001100_infra_webhook_store.sql"
    main_py = read(root / "app" / "main.py")

    assert webhook_migration.exists()
    assert "CREATE TABLE IF NOT EXISTS infra_webhook_events" in read(webhook_migration)
    assert "PRIMARY KEY (provider, event_id)" in read(webhook_migration)
    assert "from infra.plugins.webhooks import SqlWebhookStore, install_webhook_routes" in main_py
    assert "from contextlib import asynccontextmanager" in main_py
    assert "async def lifespan(app: FastAPI)" in main_py
    assert 'app = FastAPI(title="billing_api", lifespan=lifespan)' in main_py
    assert "webhooks = infra.get(WEBHOOKS_SERVICE)" in main_py
    assert "install_webhook_routes(app, webhooks, store=store)" in main_py
    assert "app.state.webhook_routes_installed = True" in main_py


def test_create_project_tasks_production_profile_adds_redis_database(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("tasks",))

    local_toml = read(root / "infra.toml")
    production_toml = read(root / "infra.production.example.toml")

    assert "[infra.plugins.tasks]\nenabled = true" in local_toml
    assert "[infra.plugins.database]\nenabled = false" in local_toml
    assert "[infra.plugins.tasks]\nenabled = true" in production_toml
    assert "[infra.plugins.database]\nenabled = true" in production_toml
    assert "mysql_enabled = false" in production_toml
    assert "mysql_host" not in production_toml
    assert "redis_enabled = true" in production_toml
    assert 'redis_url = { "$env" = "REDIS_URL" }' in production_toml
    assert "Production profile plugins: database, tasks" in read(root / "README.md")

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    settings = load_infra_settings(root / "infra.production.example.toml")
    report = build_release_check_report(settings, require_provider_certification=False)

    assert report["ready"] is True


def test_create_project_ratelimit_production_profile_adds_redis_database(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("ratelimit",))

    local_toml = read(root / "infra.toml")
    production_toml = read(root / "infra.production.example.toml")

    assert "[infra.plugins.ratelimit]\nenabled = true" in local_toml
    assert "[infra.plugins.database]\nenabled = false" in local_toml
    assert "[infra.plugins.ratelimit]\nenabled = true" in production_toml
    assert 'default_provider = "redis"' in production_toml
    assert "[infra.plugins.ratelimit.config.providers.redis]" in production_toml
    assert "[infra.plugins.database]\nenabled = true" in production_toml
    assert "mysql_enabled = false" in production_toml
    assert "redis_enabled = true" in production_toml
    assert "Production profile plugins: database, ratelimit" in read(root / "README.md")

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    settings = load_infra_settings(root / "infra.production.example.toml")
    report = build_release_check_report(settings, require_provider_certification=False)

    assert report["ready"] is True


def test_create_project_ratelimit_generates_limited_route(tmp_path):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("ratelimit",))

    main_py = read(root / "app" / "main.py")
    health_test = read(root / "tests" / "test_health.py")

    assert "from fastapi import Depends, FastAPI" in main_py
    assert "from infra.plugins import RATELIMIT_SERVICE" in main_py
    assert "from infra.plugins.ratelimit import rate_limit" in main_py
    assert '@app.get(\n    "/limited"' in main_py
    assert "Depends(rate_limit(limit=2, window_seconds=60, service=RATELIMIT_SERVICE))" in main_py
    assert "def test_rate_limited_route_blocks_after_limit" in health_test
    assert "blocked.status_code == 429" in health_test

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_create_project_adds_package_extras_for_requested_plugins(tmp_path):
    create_project(
        tmp_path / "service",
        "billing_api",
        enabled_plugins=("database", "http", "tasks"),
    )

    pyproject = read(tmp_path / "service" / "pyproject.toml")

    assert '"fastapi-infra[http,mysql,redis,tasks-redis]"' in pyproject


def test_create_project_package_extras_come_from_plugin_manifest(tmp_path):
    enabled_plugins = (
        "ai",
        "speech",
        "database",
        "cache",
        "http",
        "observability",
        "tasks",
    )
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=enabled_plugins)
    manifest = PluginManager(
        settings=load_infra_settings(root / "infra.toml"), plugins=get_builtin_plugins()
    ).manifest()
    expected_extras = sorted(
        {
            extra
            for plugin_name in enabled_plugins
            for extra in manifest[plugin_name]["recommended_extras"]
        }
    )

    pyproject = read(root / "pyproject.toml")

    assert f'"fastapi-infra[{",".join(expected_extras)}]"' in pyproject


def test_create_project_env_example_lists_enabled_provider_secrets(tmp_path):
    create_project(
        tmp_path / "service",
        "billing_api",
        enabled_plugins=(
            "ai",
            "speech",
            "payment",
            "storage",
            "database",
            "cache",
            "tasks",
            "auth",
            "notifications",
        ),
    )

    env_example = read(tmp_path / "service" / ".env.example")

    assert "OPENAI_API_KEY=" in env_example
    assert env_example.count("OPENAI_API_KEY=") == 1
    assert "ANTHROPIC_API_KEY=" in env_example
    assert "GEMINI_API_KEY=" in env_example
    assert "STRIPE_API_KEY=sk_test_example" in env_example
    assert "STRIPE_WEBHOOK_SECRET=whsec_example" in env_example
    assert "S3_LIVE_BUCKET=" in env_example
    assert "S3_LIVE_BUCKET=bucket" in env_example
    assert "S3_LIVE_REGION=us-east-1" in env_example
    assert "S3_LIVE_ACCESS_KEY_ID=access-key" in env_example
    assert "S3_LIVE_SECRET_ACCESS_KEY=secret-key" in env_example
    assert "S3_LIVE_ENDPOINT_URL=https://s3.example.test" in env_example
    assert "SMTP_HOST=smtp.example.test" in env_example
    assert "SMTP_SENDER=noreply@example.test" in env_example
    assert "JWT_SECRET=replace-with-32-byte-random-secret" in env_example
    assert "MYSQL_HOST=127.0.0.1" in env_example
    assert "MYSQL_PORT=3306" in env_example
    assert "MYSQL_USER=root" in env_example
    assert "MYSQL_PASSWORD=local-password" in env_example
    assert "MYSQL_DATABASE=billing_api" in env_example
    assert "REDIS_URL=redis://localhost:6379/0" in env_example
    assert env_example.count("REDIS_URL=") == 1
    assert "SMTP_HOST=" in env_example
    assert "WEBHOOK_NOTIFICATION_URL=" in env_example
    assert "WEBHOOK_NOTIFICATION_HEALTH_URL=" in env_example
    assert "WEBHOOK_NOTIFICATION_SIGNING_SECRET=" in env_example


def test_create_project_env_example_covers_enabled_plugin_manifest_env_vars(tmp_path):
    enabled_plugins = (
        "ai",
        "speech",
        "auth",
        "database",
        "cache",
        "http",
        "observability",
        "tasks",
        "storage",
        "webhooks",
        "payment",
        "ratelimit",
        "notifications",
    )
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=enabled_plugins)
    manifest = PluginManager(
        settings=load_infra_settings(root / "infra.toml"), plugins=get_builtin_plugins()
    ).manifest()

    expected_env_vars = {
        env_var for plugin_name in enabled_plugins for env_var in manifest[plugin_name]["env_vars"]
    }
    actual_env_vars = env_names_from_example(read(root / ".env.example"))

    assert expected_env_vars <= actual_env_vars


def test_create_project_adds_migrations_directory_when_database_enabled(tmp_path):
    root = tmp_path / "service"

    created = create_project(root, "billing_api", enabled_plugins=("database",))

    migration = root / "migrations/00000000000100_app_documents.sql"
    assert migration.exists()
    assert "CREATE TABLE IF NOT EXISTS infra_documents" in read(migration)
    assert root.joinpath("infra.toml").exists()
    assert migration in created
    assert root.joinpath("infra.toml") in created
    assert "[infra.plugins.database]" in read(root / "infra.toml")
    assert "enabled = true" in read(root / "infra.toml")
    assert "fastapi-infra migrations new migrations" in read(root / "README.md")
    assert "fastapi-infra migrations migrate migrations --settings infra.toml" in read(
        root / "README.md"
    )


def test_create_project_installs_observability_only_when_enabled(tmp_path):
    default_root = tmp_path / "default"
    create_project(default_root, "billing_api")

    default_main = read(default_root / "app" / "main.py")
    assert "install_observability_routes" not in default_main
    assert "install_observability_middleware" not in default_main

    observable_root = tmp_path / "observable"
    create_project(
        observable_root,
        "billing_api",
        enabled_plugins=("observability",),
    )

    observable_main = read(observable_root / "app" / "main.py")
    assert (
        "from infra.plugins.observability import "
        "install_observability_middleware, install_observability_routes"
    ) in observable_main
    assert 'install_observability_routes(app, infra, prefix="/ops")' in observable_main
    assert "install_observability_middleware(app)" in observable_main


def test_create_project_generated_app_imports_and_serves_health(tmp_path):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("observability",))
    try:
        from fastapi.testclient import TestClient

        with TestClient(load_generated_app(root)) as client:
            health = client.get("/health")
            readyz = client.get("/ops/readyz")

        assert health.status_code == 200
        assert readyz.status_code == 200
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "app" or module_name.startswith("app."):
                sys.modules.pop(module_name, None)


def test_create_project_generated_app_installs_webhook_route(tmp_path):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("webhooks",))
    try:
        from fastapi.testclient import TestClient

        with TestClient(load_generated_app(root)) as client:
            response = client.post("/webhooks/unknown", content=b"{}")

        assert response.status_code == 404
        assert response.json() == {"status": "unknown_provider"}
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "app" or module_name.startswith("app."):
                sys.modules.pop(module_name, None)


def test_create_project_generated_tests_pass_in_project(tmp_path):
    root = tmp_path / "service"
    create_project(
        root,
        "billing_api",
        enabled_plugins=(
            "auth",
            "cache",
            "database",
            "http",
            "notifications",
            "payment",
            "tasks",
            "storage",
        ),
    )
    main_py = read(root / "app" / "main.py")
    assert (
        "from infra.plugins import CACHE_SERVICE, DATABASE_SERVICE, HTTP_SERVICE, "
        "NOTIFICATIONS_SERVICE, PAYMENT_SERVICE, STORAGE_SERVICE, TASKS_SERVICE, "
        "WEBHOOKS_SERVICE"
    ) in main_py
    assert "from infra.plugins.webhooks import SqlWebhookStore, install_webhook_routes" in main_py
    assert "from contextlib import asynccontextmanager" in main_py
    assert "async def lifespan(app: FastAPI)" in main_py
    assert 'app = FastAPI(title="billing_api", lifespan=lifespan)' in main_py
    assert "webhooks = infra.get(WEBHOOKS_SERVICE)" in main_py
    assert "install_webhook_routes(app, webhooks, store=store)" in main_py
    assert "app.state.webhook_routes_installed = True" in main_py
    assert '@app.post("/cache/example")' in main_py
    assert '@app.get("/cache/example")' in main_py
    assert 'cache.set("examples:greeting"' in main_py
    assert '@app.post("/database/example")' in main_py
    assert '@app.get("/database/example")' in main_py
    assert 'database.put_document(\n        "examples",' in main_py
    assert (
        "from infra.plugins.transaction.coordinator import Operation, TransactionCoordinator"
        in main_py
    )
    assert '@app.post("/transactions/example")' in main_py
    assert 'Operation(name="create_order"' in main_py
    assert '@app.get("/http/example")' in main_py
    assert 'http.request(\n        "GET",' in main_py
    assert '@app.post("/notifications/example")' in main_py
    assert "notifications.send(" in main_py
    assert '@app.post("/payments/example")' in main_py
    assert "payment.create_checkout(" in main_py
    assert '@app.post("/storage/example")' in main_py
    assert '@app.get("/storage/example")' in main_py
    assert 'storage.put_object(key, b"hello from fastapi-infra"' in main_py
    health_test = read(root / "tests" / "test_health.py")
    assert "def test_cache_example_routes_write_and_read_value" in health_test
    assert "def test_database_example_routes_write_and_read_document" in health_test
    assert "def test_transaction_example_route_reports_success_and_compensation" in health_test
    assert "def test_http_example_route_uses_configured_client" in health_test
    assert "def test_notifications_example_route_sends_message" in health_test
    assert "def test_payment_example_route_creates_checkout" in health_test
    assert "def test_storage_example_routes_write_and_read_object" in health_test
    assert "def test_task_worker_processes_example_task_once" in health_test
    assert 'worker_module = importlib.import_module("app.worker")' in health_test
    assert "stats.completed == 1" in health_test
    assert "python -m app.worker" in read(root / "README.md")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "19 passed" in result.stdout


def test_create_project_verify_release_reports_unsafe_auth_env(tmp_path):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("auth",))
    (root / ".env").write_text(read(root / ".env.example"), encoding="utf-8")

    result = subprocess.run(
        ["scripts/verify-release.sh", ".env"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "unsafe JWT_SECRET in .env" in result.stderr
    assert "replace it with a random secret of at least 32 characters" in result.stderr
    assert "auth.weak_jwt_secret" not in result.stderr


def test_create_project_generated_settings_honor_infra_settings_env(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "service"
    create_project(root, "billing_api")
    override_path = root / "custom.toml"
    override_path.write_text(
        """
[infra.plugins.auth]
enabled = true

[infra.plugins.auth.config]
jwt_secret = "secret"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("INFRA_SETTINGS", str(override_path))

    spec = importlib.util.spec_from_file_location(
        "generated_settings_override",
        root / "app" / "settings.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    settings = module.build_settings()

    assert settings.get_plugin("auth").enabled is True
    assert settings.get_plugin("auth").config["jwt_secret"] == "secret"


def test_create_project_adds_worker_only_when_tasks_enabled(tmp_path):
    default_root = tmp_path / "default"
    create_project(default_root, "billing_api")

    assert not default_root.joinpath("app/worker.py").exists()

    tasks_root = tmp_path / "tasks"
    created = create_project(
        tasks_root,
        "billing_api",
        enabled_plugins=("tasks",),
    )

    worker_path = tasks_root / "app" / "worker.py"
    worker_py = read(worker_path)
    assert worker_path in created
    assert "TaskWorkerRunConfig" in worker_py
    assert "run_task_worker" in worker_py
    assert "settings: InfraSettings = build_settings()" in worker_py
    assert "from infra.plugins import TASKS_SERVICE" in worker_py
    assert "queue = infra.require(TASKS_SERVICE)" in worker_py
    assert "OBSERVABILITY_SERVICE" not in worker_py
    assert "instrumentation=" not in worker_py
    assert '@worker.handler("example.ping")' in worker_py
    assert "require_handlers=True" in worker_py
    assert "python -m app.worker" in read(tasks_root / "README.md")
    assert "fake" not in worker_py.lower()


def test_create_project_task_worker_uses_observability_when_enabled(tmp_path):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("tasks", "observability"))

    worker_py = read(root / "app" / "worker.py")
    health_test = read(root / "tests" / "test_health.py")

    assert "from infra.plugins import OBSERVABILITY_SERVICE, TASKS_SERVICE" in worker_py
    assert "instrumentation = infra.get(OBSERVABILITY_SERVICE)" in worker_py
    assert "TaskWorker(queue, instrumentation=instrumentation)" in worker_py
    assert 'counters["task_worker_tasks_total"] == 1' in health_test
    assert 'counters["task_worker_completed_total"] == 1' in health_test
    assert 'timers["task_worker_task_seconds"]' in health_test

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_create_project_generated_settings_activate_only_requested_plugins(tmp_path):
    root = tmp_path / "service"
    create_project(root, "billing_api", enabled_plugins=("auth", "tasks"))

    spec = importlib.util.spec_from_file_location(
        "generated_settings",
        root / "app" / "settings.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    manager = PluginManager(settings=module.build_settings(), plugins=get_builtin_plugins())

    await manager.startup()

    assert set(manager.health.snapshot()) >= {"auth", "tasks", "ai", "payment"}
    assert manager.get("auth") is not None
    assert manager.get("tasks") is not None
    assert manager.get("ai") is None
    assert manager.get("payment") is None

    await manager.shutdown()


def test_create_project_refuses_non_empty_destination_without_overwrite(tmp_path):
    destination = tmp_path / "service"
    destination.mkdir()
    (destination / "existing.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        create_project(destination, "billing_api")

    assert read(destination / "existing.txt") == "keep me"


def test_create_project_overwrite_replaces_generated_files(tmp_path):
    destination = tmp_path / "service"
    create_project(destination, "billing_api")
    (destination / "app" / "main.py").write_text("stale", encoding="utf-8")

    create_project(destination, "billing_api", overwrite=True)

    assert "stale" not in read(destination / "app" / "main.py")
    assert 'app = FastAPI(title="billing_api")' in read(destination / "app" / "main.py")


def test_create_project_overwrite_removes_stale_optional_generated_files(tmp_path):
    destination = tmp_path / "service"
    create_project(destination, "billing_api", enabled_plugins=("tasks", "database"))

    assert destination.joinpath("app/worker.py").exists()
    assert destination.joinpath("migrations/00000000000100_app_documents.sql").exists()

    create_project(destination, "billing_api", overwrite=True)

    assert not destination.joinpath("app/worker.py").exists()
    assert not destination.joinpath("migrations/00000000000100_app_documents.sql").exists()
    assert not destination.joinpath("migrations").exists()
    assert "Enabled plugins: none" in read(destination / "README.md")


def test_create_project_overwrite_preserves_modified_optional_files(tmp_path):
    destination = tmp_path / "service"
    create_project(destination, "billing_api", enabled_plugins=("tasks", "database"))
    destination.joinpath("app/worker.py").write_text("# custom worker\n", encoding="utf-8")
    destination.joinpath("migrations/custom.sql").write_text("SELECT 1;\n", encoding="utf-8")

    create_project(destination, "billing_api", overwrite=True)

    assert read(destination / "app" / "worker.py") == "# custom worker\n"
    assert destination.joinpath("migrations/custom.sql").exists()
    assert not destination.joinpath("migrations/.gitkeep").exists()


def test_create_project_overwrite_removes_unmodified_generated_plugin_migrations(tmp_path):
    destination = tmp_path / "service"
    create_project(destination, "billing_api", enabled_plugins=("payment", "webhooks"))

    assert destination.joinpath("migrations/00000000001000_infra_payment_store.sql").exists()
    assert destination.joinpath("migrations/00000000001100_infra_webhook_store.sql").exists()

    create_project(destination, "billing_api", overwrite=True)

    assert not destination.joinpath("migrations/00000000001000_infra_payment_store.sql").exists()
    assert not destination.joinpath("migrations/00000000001100_infra_webhook_store.sql").exists()
    assert not destination.joinpath("migrations").exists()


def test_create_project_overwrite_preserves_modified_generated_plugin_migrations(tmp_path):
    destination = tmp_path / "service"
    create_project(destination, "billing_api", enabled_plugins=("payment",))
    migration = destination / "migrations/00000000001000_infra_payment_store.sql"
    migration.write_text("-- customized payment schema\n", encoding="utf-8")

    create_project(destination, "billing_api", overwrite=True)

    assert read(migration) == "-- customized payment schema\n"


@pytest.mark.parametrize(
    "project_name",
    ["BillingApi", "billing api", "../billing", "billing.api", "", "-billing"],
)
def test_create_project_rejects_invalid_project_name(tmp_path, project_name):
    with pytest.raises(ValueError):
        create_project(tmp_path / "service", project_name)


def test_create_project_rejects_unknown_or_unsafe_plugin_names(tmp_path):
    with pytest.raises(ValueError, match="Run fastapi-infra plugins"):
        create_project(tmp_path / "service", "billing_api", enabled_plugins=("evil",))

    with pytest.raises(ValueError, match="available plugins"):
        create_project(
            tmp_path / "service2",
            "billing_api",
            enabled_plugins=('auth": {"enabled": True}, "payment',),
        )
