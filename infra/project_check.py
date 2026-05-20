from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Severity = Literal["error", "warning"]

REQUIRED_PROJECT_FILES: tuple[str, ...] = (
    "AGENTS.md",
    ".dockerignore",
    ".gitignore",
    "pyproject.toml",
    "app/main.py",
    "app/settings.py",
    "tests/test_config.py",
    "tests/test_health.py",
    "Dockerfile",
    "Makefile",
    "compose.yaml",
    "README.md",
    ".env.example",
    "provider.env.example",
    "infra.toml",
    "infra.production.example.toml",
    "infra.manifest.json",
    "scripts/prepare-env.sh",
    "scripts/verify-release.sh",
    ".github/workflows/ci.yml",
)

REQUIRED_CI_GATES: tuple[str, ...] = (
    'pip install -e ".[dev]"',
    "make env",
    "make verify",
    "make release-static",
)

REQUIRED_DOCKERFILE_GATES: tuple[str, ...] = (
    "FROM python:3.11-slim",
    "ENV INFRA_SETTINGS=infra.toml",
    "COPY pyproject.toml README.md infra.toml infra.production.example.toml infra.manifest.json ./",
    "COPY app ./app",
    "COPY scripts ./scripts",
    "pip install --no-cache-dir .",
    "chmod +x scripts/*.sh",
    'adduser --disabled-password --gecos "" appuser',
    "USER appuser",
    "HEALTHCHECK",
    "urllib.request.urlopen('http://127.0.0.1:8000/health'",
    'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]',
)

REQUIRED_DOCKERIGNORE_GATES: tuple[str, ...] = (
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "*.egg-info",
    "*.pyc",
    ".env",
    "provider.env",
    "provider-env-template.env",
    "provider-certification.json",
    "provider-preflight.json",
)

REQUIRED_GITIGNORE_GATES: tuple[str, ...] = (
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".coverage",
    "htmlcov",
    "dist",
    "*.egg-info",
    "*.pyc",
    ".env",
    "provider.env",
    "provider-env-template.env",
    "provider-certification.json",
    "provider-preflight.json",
)

REQUIRED_COMPOSE_GATES: tuple[str, ...] = (
    "services:",
    "  app:",
    "    build: .",
    "    env_file:",
    "      - .env",
    "      INFRA_SETTINGS: infra.production.example.toml",
    '      - "8000:8000"',
)

REQUIRED_MAKEFILE_GATES: tuple[str, ...] = (
    ".PHONY:",
    "install:",
    'pip install -e ".[dev]"',
    "run:",
    "uvicorn app.main:app --reload",
    "test:",
    "python -m pytest -q",
    "config-check:",
    "fastapi-infra config-check --settings infra.toml",
    "project-check:",
    "fastapi-infra project-check .",
    "verify: config-check project-check test",
    "env:",
    "scripts/prepare-env.sh $(RUNTIME_ENV_FILE) $(PROVIDER_ENV_FILE)",
    "release-static:",
    "fastapi-infra config-check --settings infra.production.example.toml --env-file $(RUNTIME_ENV_FILE)",
    "fastapi-infra release-check --settings infra.production.example.toml --env-file $(RUNTIME_ENV_FILE)",
    "--static-only",
    "provider-list:",
    "fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file $(RUNTIME_ENV_FILE) --list --requirements",
    "provider-preflight:",
    "fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file $(RUNTIME_ENV_FILE) --preflight --env-file $(PROVIDER_ENV_FILE)",
    "release:",
    "scripts/verify-release.sh $(RUNTIME_ENV_FILE) $(PROVIDER_ENV_FILE)",
    "dev-up:",
    "docker compose up --build",
    "dev-down:",
    "docker compose down",
    "docker-build:",
    "docker build -t app .",
)

REQUIRED_MANIFEST_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("docker", ("make dev-up",)),
    ("prepare_env", ("make env",)),
    ("install", ('pip install -e ".[dev]"',)),
    ("local_verify", ("make verify",)),
    ("production_static", ("make release-static",)),
    ("provider_preflight", ("make provider-preflight",)),
    ("release_script", ("scripts/verify-release.sh .env provider.env",)),
)


@dataclass(frozen=True)
class ProjectCheckIssue:
    code: str
    message: str
    severity: Severity = "error"
    path: str | None = None


def build_project_check_report(project_path: str | Path) -> dict[str, Any]:
    root = Path(project_path)
    issues: list[ProjectCheckIssue] = []

    if not root.exists():
        _error(issues, "project_missing", f"project path does not exist: {root}", str(root))
        return _report(issues, manifest=None)
    if not root.is_dir():
        _error(
            issues, "project_not_directory", f"project path is not a directory: {root}", str(root)
        )
        return _report(issues, manifest=None)

    manifest = _load_manifest(root, issues)
    for relative_path in REQUIRED_PROJECT_FILES:
        _require_file(root, relative_path, issues)

    if manifest is None:
        return _report(issues, manifest=None)

    _check_manifest_shape(manifest, issues)
    _check_manifest_commands(manifest, issues)
    _check_manifest_plugins(manifest, issues)
    _check_declared_files(root, manifest, issues)
    _check_pyproject_dependencies(root, manifest, issues)
    _check_agents_file(root, manifest, issues)
    _check_config_plugins(
        root,
        "infra.toml",
        tuple(_string_list(manifest.get("enabled_plugins", []))),
        issues,
    )
    _check_config_plugins(
        root,
        "infra.production.example.toml",
        tuple(_string_list(manifest.get("production_plugins", []))),
        issues,
    )
    _check_makefile(root, manifest, issues)
    _check_compose_file(root, manifest, issues)
    _check_dockerignore(root, manifest, issues)
    _check_gitignore(root, manifest, issues)
    _check_dockerfile(root, manifest, issues)
    _check_ci_workflow(root, manifest, issues)
    _check_prepare_env_script(root, manifest, issues)
    _check_release_script(root, manifest, issues)
    _check_migrations(root, manifest, issues)

    return _report(issues, manifest=manifest)


def format_project_check_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def format_project_check_text(report: Mapping[str, Any]) -> str:
    lines = [
        "project-check: valid" if report["valid"] else "project-check: invalid",
        f"errors: {report['summary']['errors']}",
        f"warnings: {report['summary']['warnings']}",
    ]
    for issue in report["issues"]:
        path = f" {issue['path']}:" if issue.get("path") else ""
        lines.append(f"{issue['severity']}{path} {issue['code']}: {issue['message']}")
    return "\n".join(lines)


def _load_manifest(root: Path, issues: list[ProjectCheckIssue]) -> Mapping[str, Any] | None:
    path = root / "infra.manifest.json"
    if not path.exists():
        _error(
            issues,
            "manifest_missing",
            "infra.manifest.json is required for generated project audit",
            "infra.manifest.json",
        )
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _error(issues, "manifest_invalid", f"could not read infra.manifest.json: {exc}", str(path))
        return None
    if not isinstance(data, Mapping):
        _error(
            issues, "manifest_invalid", "infra.manifest.json must contain a JSON object", str(path)
        )
        return None
    return data


def _check_manifest_shape(
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    if manifest.get("schema_version") != 1:
        _error(issues, "manifest_schema_version", "schema_version must be 1", "infra.manifest.json")
    if manifest.get("generator") != "fastapi-infra":
        _error(
            issues, "manifest_generator", "generator must be fastapi-infra", "infra.manifest.json"
        )
    for key in (
        "project_name",
        "profile",
        "enabled_plugins",
        "production_plugins",
        "package_plugins",
        "commands",
        "plugins",
    ):
        if key not in manifest:
            _error(
                issues, "manifest_missing_key", f"manifest is missing {key}", "infra.manifest.json"
            )
    if (
        not _string_list(manifest.get("enabled_plugins", []))
        and manifest.get("profile") != "minimal"
    ):
        _warn(
            issues,
            "manifest_empty_enabled_plugins",
            "non-minimal profile has no enabled plugins",
            "infra.manifest.json",
        )


def _check_manifest_plugins(
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    enabled = _manifest_string_list(manifest, "enabled_plugins", issues)
    production = _manifest_string_list(manifest, "production_plugins", issues)
    package = _manifest_string_list(manifest, "package_plugins", issues)
    expected_package = tuple(dict.fromkeys((*enabled, *production)))
    if package != expected_package:
        _error(
            issues,
            "manifest_package_plugins_mismatch",
            "package_plugins are "
            + _display_list(package)
            + " but expected "
            + _display_list(expected_package),
            "infra.manifest.json",
        )

    plugin_entries = manifest.get("plugins", [])
    if not isinstance(plugin_entries, list):
        _error(issues, "manifest_plugins_invalid", "plugins must be a list", "infra.manifest.json")
        return

    plugin_names = []
    for index, entry in enumerate(plugin_entries):
        if not isinstance(entry, Mapping):
            _error(
                issues,
                "manifest_plugin_entry_invalid",
                f"plugins[{index}] must be an object",
                "infra.manifest.json",
            )
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            _error(
                issues,
                "manifest_plugin_entry_invalid",
                f"plugins[{index}].name must be a string",
                "infra.manifest.json",
            )
            continue
        plugin_names.append(name)
        requested = entry.get("requested")
        if requested is not (name in enabled):
            _error(
                issues,
                "manifest_plugin_requested_mismatch",
                f"plugins[{index}].requested for {name} does not match enabled_plugins",
                "infra.manifest.json",
            )
        production_enabled = entry.get("production_enabled")
        if production_enabled is not (name in production):
            _error(
                issues,
                "manifest_plugin_production_mismatch",
                f"plugins[{index}].production_enabled for {name} does not match production_plugins",
                "infra.manifest.json",
            )
        for key in (
            "services",
            "env_vars",
            "recommended_extras",
            "production_dependencies",
        ):
            if not isinstance(entry.get(key, []), list) or not all(
                isinstance(item, str) for item in entry.get(key, [])
            ):
                _error(
                    issues,
                    "manifest_plugin_field_invalid",
                    f"plugins[{index}].{key} must be a list of strings",
                    "infra.manifest.json",
                )

    if tuple(plugin_names) != package:
        _error(
            issues,
            "manifest_plugins_mismatch",
            "plugin entries are "
            + _display_list(tuple(plugin_names))
            + " but package_plugins are "
            + _display_list(package),
            "infra.manifest.json",
        )


def _check_declared_files(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    files = manifest.get("files", {})
    if not isinstance(files, Mapping):
        _error(issues, "manifest_files_invalid", "files must be an object", "infra.manifest.json")
        return
    for label, relative_path in files.items():
        if not isinstance(label, str) or not isinstance(relative_path, str):
            _error(
                issues,
                "manifest_file_entry_invalid",
                "files entries must map string labels to string paths",
                "infra.manifest.json",
            )
            continue
        _require_file(root, relative_path, issues)


def _check_manifest_commands(
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    commands = manifest.get("commands", {})
    if not isinstance(commands, Mapping):
        _error(
            issues,
            "manifest_commands_invalid",
            "commands must be an object",
            "infra.manifest.json",
        )
        return
    for label, expected_commands in REQUIRED_MANIFEST_COMMANDS:
        actual_commands = commands.get(label)
        if actual_commands is None:
            _error(
                issues,
                "manifest_command_missing",
                f"manifest commands is missing {label}",
                "infra.manifest.json",
            )
            continue
        if not isinstance(actual_commands, list) or not all(
            isinstance(command, str) for command in actual_commands
        ):
            _error(
                issues,
                "manifest_command_invalid",
                f"manifest command {label} must be a list of strings",
                "infra.manifest.json",
            )
            continue
        if tuple(actual_commands) != expected_commands:
            _error(
                issues,
                "manifest_command_mismatch",
                f"manifest command {label} is {_display_list(tuple(actual_commands))} "
                f"but expected {_display_list(expected_commands)}",
                "infra.manifest.json",
            )


def _check_agents_file(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    files = manifest.get("files", {})
    relative_path = "AGENTS.md"
    if isinstance(files, Mapping) and isinstance(files.get("agent_instructions"), str):
        relative_path = files["agent_instructions"]
    path = root / relative_path
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    for expected in (
        "Use `infra.manifest.json` as the project contract.",
        "Run `make env` before production checks.",
        "Run `make verify` before handing off changes.",
        "Run `make release-static` for static production readiness.",
        "Keep runtime `.env` separate from provider `provider.env`.",
        "Do not run live provider certification unless explicitly requested.",
    ):
        if expected not in content:
            _error(
                issues,
                "agents_file_missing_gate",
                f"AGENTS.md is missing: {expected}",
                relative_path,
            )


def _check_pyproject_dependencies(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    path = root / "pyproject.toml"
    if not path.exists():
        return
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _error(
            issues, "pyproject_invalid", f"could not parse pyproject.toml: {exc}", "pyproject.toml"
        )
        return

    project = data.get("project", {})
    dependencies = project.get("dependencies", []) if isinstance(project, Mapping) else []
    if not isinstance(dependencies, list) or not all(
        isinstance(dependency, str) for dependency in dependencies
    ):
        _error(
            issues,
            "pyproject_dependencies_invalid",
            "project.dependencies must be a list of strings",
            "pyproject.toml",
        )
        return

    expected_extras = _expected_package_extras(manifest)
    expected_dependency = (
        f"fastapi-infra[{','.join(expected_extras)}]" if expected_extras else "fastapi-infra"
    )
    fastapi_infra_dependencies = [
        dependency
        for dependency in dependencies
        if dependency == "fastapi-infra" or dependency.startswith("fastapi-infra[")
    ]
    if fastapi_infra_dependencies != [expected_dependency]:
        _error(
            issues,
            "pyproject_dependency_mismatch",
            "pyproject.toml fastapi-infra dependency is "
            + _display_list(tuple(fastapi_infra_dependencies))
            + " but expected "
            + expected_dependency,
            "pyproject.toml",
        )
    if not any(dependency.startswith("uvicorn[standard]") for dependency in dependencies):
        _error(
            issues,
            "pyproject_missing_uvicorn",
            "pyproject.toml must depend on uvicorn[standard]",
            "pyproject.toml",
        )


def _check_config_plugins(
    root: Path,
    relative_path: str,
    expected_enabled: tuple[str, ...],
    issues: list[ProjectCheckIssue],
) -> None:
    path = root / relative_path
    if not path.exists():
        return
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _error(issues, "config_invalid", f"could not parse {relative_path}: {exc}", relative_path)
        return
    plugins = data.get("infra", {}).get("plugins", {})
    if not isinstance(plugins, Mapping):
        _error(issues, "config_plugins_invalid", "infra.plugins must be an object", relative_path)
        return
    actual_enabled = tuple(
        name
        for name, plugin in plugins.items()
        if isinstance(name, str) and isinstance(plugin, Mapping) and plugin.get("enabled") is True
    )
    if set(actual_enabled) != set(expected_enabled):
        _error(
            issues,
            "config_plugin_mismatch",
            "enabled plugins are "
            + _display_list(actual_enabled)
            + " but manifest expects "
            + _display_list(expected_enabled),
            relative_path,
        )


def _check_release_script(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    files = manifest.get("files", {})
    relative_path = "scripts/verify-release.sh"
    if isinstance(files, Mapping) and isinstance(files.get("release_script"), str):
        relative_path = files["release_script"]
    path = root / relative_path
    if not path.exists():
        return
    if os.access(path, os.X_OK) is False:
        _error(
            issues,
            "release_script_not_executable",
            "release script must be executable",
            relative_path,
        )
    content = path.read_text(encoding="utf-8")
    for expected in (
        "make verify",
        'RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" make release-static',
        'RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" make provider-list',
        "make provider-preflight",
        "RUN_LIVE_CERTIFICATION",
        "provider-certification.json",
    ):
        if expected not in content:
            _error(
                issues,
                "release_script_missing_gate",
                f"release script is missing: {expected}",
                relative_path,
            )


def _check_prepare_env_script(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    files = manifest.get("files", {})
    relative_path = "scripts/prepare-env.sh"
    if isinstance(files, Mapping) and isinstance(files.get("prepare_env_script"), str):
        relative_path = files["prepare_env_script"]
    path = root / relative_path
    if not path.exists():
        return
    if os.access(path, os.X_OK) is False:
        _error(
            issues,
            "prepare_env_script_not_executable",
            "prepare env script must be executable",
            relative_path,
        )
    content = path.read_text(encoding="utf-8")
    for expected in (
        'cp .env.example "$RUNTIME_ENV_FILE"',
        "secrets.token_urlsafe(32)",
        "JWT_SECRET",
        'cp provider.env.example "$PROVIDER_ENV_FILE"',
    ):
        if expected not in content:
            _error(
                issues,
                "prepare_env_script_missing_gate",
                f"prepare env script is missing: {expected}",
                relative_path,
            )


def _check_dockerfile(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    path = root / "Dockerfile"
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    for expected in REQUIRED_DOCKERFILE_GATES:
        if expected not in content:
            _error(
                issues,
                "dockerfile_missing_gate",
                f"Dockerfile is missing: {expected}",
                "Dockerfile",
            )
    production_plugins = set(_string_list(manifest.get("production_plugins", [])))
    if "database" in production_plugins and "COPY migrations ./migrations" not in content:
        _error(
            issues,
            "dockerfile_missing_migrations",
            "production database profile requires Dockerfile to copy migrations",
            "Dockerfile",
        )


def _check_dockerignore(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    files = manifest.get("files", {})
    relative_path = ".dockerignore"
    if isinstance(files, Mapping) and isinstance(files.get("dockerignore"), str):
        relative_path = files["dockerignore"]
    path = root / relative_path
    if not path.exists():
        return
    patterns = _ignore_patterns(path)
    for expected in REQUIRED_DOCKERIGNORE_GATES:
        if expected not in patterns:
            _error(
                issues,
                "dockerignore_missing_gate",
                f".dockerignore is missing: {expected}",
                relative_path,
            )


def _ignore_patterns(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _check_gitignore(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    files = manifest.get("files", {})
    relative_path = ".gitignore"
    if isinstance(files, Mapping) and isinstance(files.get("gitignore"), str):
        relative_path = files["gitignore"]
    path = root / relative_path
    if not path.exists():
        return
    patterns = _ignore_patterns(path)
    for expected in REQUIRED_GITIGNORE_GATES:
        if expected not in patterns:
            _error(
                issues,
                "gitignore_missing_gate",
                f".gitignore is missing: {expected}",
                relative_path,
            )


def _check_makefile(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    files = manifest.get("files", {})
    relative_path = "Makefile"
    if isinstance(files, Mapping) and isinstance(files.get("makefile"), str):
        relative_path = files["makefile"]
    path = root / relative_path
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    for expected in REQUIRED_MAKEFILE_GATES:
        if expected not in content:
            _error(
                issues,
                "makefile_missing_gate",
                f"Makefile is missing: {expected}",
                relative_path,
            )
    production_plugins = set(_string_list(manifest.get("production_plugins", [])))
    if "database" in production_plugins and "--migrations migrations" not in content:
        _error(
            issues,
            "makefile_missing_migration_gate",
            "production database profile requires release-static to include --migrations migrations",
            relative_path,
        )


def _check_compose_file(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    files = manifest.get("files", {})
    relative_path = "compose.yaml"
    if isinstance(files, Mapping) and isinstance(files.get("compose"), str):
        relative_path = files["compose"]
    path = root / relative_path
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    for expected in REQUIRED_COMPOSE_GATES:
        if expected not in content:
            _error(
                issues,
                "compose_missing_gate",
                f"compose file is missing: {expected}",
                relative_path,
            )
    dependencies = _compose_dependencies_for_project(root)
    if dependencies["mysql"]:
        for expected in (
            "  mysql:",
            "      MYSQL_HOST: mysql",
            "        condition: service_healthy",
            "      MYSQL_ROOT_PASSWORD: ${MYSQL_PASSWORD:-local-password}",
            '      test: ["CMD-SHELL", "mysqladmin ping',
            "  mysql_data:",
        ):
            if expected not in content:
                _error(
                    issues,
                    "compose_missing_mysql",
                    f"compose file is missing MySQL dependency wiring: {expected}",
                    relative_path,
                )
    if dependencies["redis"]:
        for expected in (
            "  redis:",
            "      REDIS_URL: redis://redis:6379/0",
            "        condition: service_healthy",
            '      test: ["CMD", "redis-cli", "ping"]',
        ):
            if expected not in content:
                _error(
                    issues,
                    "compose_missing_redis",
                    f"compose file is missing Redis dependency wiring: {expected}",
                    relative_path,
                )


def _check_ci_workflow(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    files = manifest.get("files", {})
    relative_path = ".github/workflows/ci.yml"
    if isinstance(files, Mapping) and isinstance(files.get("ci_workflow"), str):
        relative_path = files["ci_workflow"]
    path = root / relative_path
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    for expected in REQUIRED_CI_GATES:
        if expected not in content:
            _error(
                issues,
                "ci_workflow_missing_gate",
                f"CI workflow is missing: {expected}",
                relative_path,
            )
    production_plugins = set(_string_list(manifest.get("production_plugins", [])))
    if "database" in production_plugins and "make release-static" not in content:
        _error(
            issues,
            "ci_workflow_missing_migration_gate",
            "production database profile requires CI to use make release-static",
            relative_path,
        )


def _compose_dependencies_for_project(root: Path) -> dict[str, bool]:
    path = root / "infra.production.example.toml"
    if not path.exists():
        return {"mysql": False, "redis": False}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {"mysql": False, "redis": False}
    plugins = data.get("infra", {}).get("plugins", {})
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


def _check_migrations(
    root: Path,
    manifest: Mapping[str, Any],
    issues: list[ProjectCheckIssue],
) -> None:
    production_plugins = set(_string_list(manifest.get("production_plugins", [])))
    if "database" not in production_plugins:
        return
    migrations = root / "migrations"
    if not migrations.is_dir():
        _error(
            issues,
            "migrations_missing",
            "production database profile requires a migrations directory",
            "migrations",
        )
        return
    if not any(migrations.glob("*.sql")) and not (migrations / ".gitkeep").exists():
        _warn(
            issues,
            "migrations_empty",
            "migrations directory exists but contains no generated SQL files",
            "migrations",
        )


def _require_file(root: Path, relative_path: str, issues: list[ProjectCheckIssue]) -> None:
    path = root / relative_path
    if path.is_file():
        return
    _error(issues, "file_missing", f"required file is missing: {relative_path}", relative_path)


def _report(
    issues: list[ProjectCheckIssue],
    *,
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    return {
        "valid": not errors,
        "summary": {
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "project": {
            "name": manifest.get("project_name") if manifest is not None else None,
            "profile": manifest.get("profile") if manifest is not None else None,
            "enabled_plugins": (
                _string_list(manifest.get("enabled_plugins", [])) if manifest is not None else []
            ),
            "production_plugins": (
                _string_list(manifest.get("production_plugins", [])) if manifest is not None else []
            ),
        },
        "issues": [
            {
                "severity": issue.severity,
                "code": issue.code,
                "message": issue.message,
                "path": issue.path,
            }
            for issue in issues
        ],
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _manifest_string_list(
    manifest: Mapping[str, Any],
    key: str,
    issues: list[ProjectCheckIssue],
) -> tuple[str, ...]:
    value = manifest.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _error(
            issues,
            "manifest_list_invalid",
            f"{key} must be a list of strings",
            "infra.manifest.json",
        )
        return ()
    return tuple(value)


def _expected_package_extras(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    extras: set[str] = set()
    plugin_entries = manifest.get("plugins", [])
    if not isinstance(plugin_entries, list):
        return ()
    for entry in plugin_entries:
        if not isinstance(entry, Mapping):
            continue
        recommended_extras = entry.get("recommended_extras", [])
        if isinstance(recommended_extras, list):
            extras.update(extra for extra in recommended_extras if isinstance(extra, str))
    return tuple(sorted(extras))


def _display_list(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "none"


def _error(
    issues: list[ProjectCheckIssue],
    code: str,
    message: str,
    path: str | None = None,
) -> None:
    issues.append(ProjectCheckIssue(code=code, message=message, path=path))


def _warn(
    issues: list[ProjectCheckIssue],
    code: str,
    message: str,
    path: str | None = None,
) -> None:
    issues.append(ProjectCheckIssue(code=code, message=message, severity="warning", path=path))
