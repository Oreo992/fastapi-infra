import importlib.util
from pathlib import Path

import pytest

from infra.scaffold import create_project
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.manager import PluginManager


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_create_project_writes_expected_files_and_imports(tmp_path):
    created = create_project(tmp_path / "service", "billing_api")

    created_relative = {path.relative_to(tmp_path / "service") for path in created}
    assert created_relative == {
        Path(".env.example"),
        Path("README.md"),
        Path("app/main.py"),
        Path("app/settings.py"),
        Path("pyproject.toml"),
    }

    main_py = read(tmp_path / "service" / "app" / "main.py")
    assert "from fastapi import FastAPI" in main_py
    assert "from infra import InfraSettings, setup_infra" in main_py
    assert "app = FastAPI(title=\"billing_api\")" in main_py

    settings_py = read(tmp_path / "service" / "app" / "settings.py")
    assert "def build_settings() -> InfraSettings:" in settings_py


def test_create_project_configures_only_requested_plugins(tmp_path):
    create_project(
        tmp_path / "service",
        "billing_api",
        enabled_plugins=("auth", "tasks"),
    )

    settings_py = read(tmp_path / "service" / "app" / "settings.py")
    assert '"auth": {"enabled": True}' in settings_py
    assert '"tasks": {"enabled": True}' in settings_py
    assert '"ai": {"enabled": False}' in settings_py
    assert '"observability": {"enabled": False}' in settings_py
    assert '"payment": {"enabled": False}' in settings_py


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
    await manager.shutdown()

    assert set(manager.health.snapshot()) >= {"auth", "tasks", "ai", "payment"}
    assert manager.get("auth") is not None
    assert manager.get("tasks") is not None
    assert manager.get("ai") is None
    assert manager.get("payment") is None


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
    assert "app = FastAPI(title=\"billing_api\")" in read(destination / "app" / "main.py")


@pytest.mark.parametrize(
    "project_name",
    ["BillingApi", "billing api", "../billing", "billing.api", "", "-billing"],
)
def test_create_project_rejects_invalid_project_name(tmp_path, project_name):
    with pytest.raises(ValueError):
        create_project(tmp_path / "service", project_name)


def test_create_project_rejects_unknown_or_unsafe_plugin_names(tmp_path):
    with pytest.raises(ValueError, match="unknown plugin name"):
        create_project(tmp_path / "service", "billing_api", enabled_plugins=("evil",))

    with pytest.raises(ValueError, match="unknown plugin name"):
        create_project(
            tmp_path / "service2",
            "billing_api",
            enabled_plugins=('auth": {"enabled": True}, "payment',),
        )
