from collections.abc import Iterable
from pathlib import Path
import re


DEFAULT_ENABLED_PLUGINS = ("ai", "auth", "observability", "tasks")
PROJECT_NAME_RE = re.compile(r"^[a-z](?:[a-z0-9_-]*[a-z0-9])?$")
PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
BUILTIN_PLUGIN_NAMES = (
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


def create_project(
    destination: str | Path,
    project_name: str,
    enabled_plugins: Iterable[str] = DEFAULT_ENABLED_PLUGINS,
    overwrite: bool = False,
) -> list[Path]:
    """Create a small FastAPI project wired to this infrastructure package."""
    if not PROJECT_NAME_RE.fullmatch(project_name):
        raise ValueError(
            "project_name must start with a lowercase letter and contain only "
            "lowercase letters, numbers, underscores, or hyphens"
        )

    root = Path(destination)
    if root.exists() and not root.is_dir():
        raise FileExistsError(f"Destination exists and is not a directory: {root}")
    if root.exists() and any(root.iterdir()) and not overwrite:
        raise FileExistsError(f"Destination exists and is not empty: {root}")

    plugins = _validate_plugins(enabled_plugins)
    files = {
        Path("pyproject.toml"): _render_pyproject(project_name),
        Path("app/main.py"): _render_main(project_name),
        Path("app/settings.py"): _render_settings(plugins),
        Path("README.md"): _render_readme(project_name, plugins),
        Path(".env.example"): _render_env_example(project_name),
    }

    written: list[Path] = []
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)

    return written


def _render_pyproject(project_name: str) -> str:
    return f"""[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "{project_name}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi-infra",
]
"""


def _render_main(project_name: str) -> str:
    return f"""from fastapi import FastAPI

from infra import InfraSettings, setup_infra

from .settings import build_settings


app = FastAPI(title="{project_name}")
settings: InfraSettings = build_settings()
infra = setup_infra(app, settings)


@app.get("/health")
async def health() -> dict[str, object]:
    return {{
        name: status.model_dump()
        for name, status in infra.health.snapshot().items()
    }}
"""


def _render_settings(enabled_plugins: Iterable[str]) -> str:
    enabled = set(enabled_plugins)
    plugin_entries = tuple(
        f'                "{plugin_name}": {{"enabled": {plugin_name in enabled}}},'
        for plugin_name in BUILTIN_PLUGIN_NAMES
    )
    plugins_block = "{\n" + "\n".join(plugin_entries) + "\n            }"

    return f"""from infra import InfraSettings


def build_settings() -> InfraSettings:
    return InfraSettings(
        infra={{
            "plugins": {plugins_block},
        }},
    )
"""


def _render_readme(project_name: str, enabled_plugins: Iterable[str]) -> str:
    plugin_list = ", ".join(enabled_plugins) or "none"
    return f"""# {project_name}

Small FastAPI app generated from `fastapi-infra`.

## Run

```bash
uvicorn app.main:app --reload
```

Enabled plugins: {plugin_list}
"""


def _render_env_example(project_name: str) -> str:
    return f"""APP_NAME={project_name}
ENVIRONMENT=local
"""


def _validate_plugins(enabled_plugins: Iterable[str]) -> tuple[str, ...]:
    plugins = tuple(enabled_plugins)
    unknown = sorted(set(plugins) - set(BUILTIN_PLUGIN_NAMES))
    if unknown:
        raise ValueError(f"unknown plugin name: {', '.join(unknown)}")
    invalid = [plugin for plugin in plugins if not PLUGIN_NAME_RE.fullmatch(plugin)]
    if invalid:
        raise ValueError(f"invalid plugin name: {', '.join(invalid)}")
    return plugins
