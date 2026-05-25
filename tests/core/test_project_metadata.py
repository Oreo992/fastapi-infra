import base64
import hashlib
import importlib
import importlib.util
import os
import re
import stat
import tarfile
import tomllib
import warnings
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Sequence, cast

from infra.config import validate_infra_settings
from infra.config.models import InfraSettings
from infra.core import ServiceKey
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.manager import PluginManager

ENV_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
ENV_EXAMPLE_VALUES = {
    "ANTHROPIC_API_KEY": "sk-ant",
    "GEMINI_API_KEY": "gemini-key",
    "JWT_SECRET": "jwt-secret",
    "MYSQL_DATABASE": "app",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PASSWORD": "mysql-password",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "root",
    "OPENAI_API_KEY": "sk-openai",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_LIVE_ACCESS_KEY_ID": "access",
    "S3_LIVE_BUCKET": "bucket",
    "S3_LIVE_ENDPOINT_URL": "https://s3.example.test",
    "S3_LIVE_REGION": "us-east-1",
    "S3_LIVE_SECRET_ACCESS_KEY": "secret",
    "SMTP_HOST": "smtp.example.test",
    "SMTP_PASSWORD": "smtp-password",
    "SMTP_PORT": "587",
    "SMTP_SENDER": "noreply@example.test",
    "SMTP_USERNAME": "smtp-user",
    "STRIPE_API_KEY": "sk-stripe",
    "STRIPE_WEBHOOK_SECRET": "whsec_test",
}
DEFAULT_METADATA_DEPENDENCIES = (
    "fastapi>=0.117.1,<0.118.0",
    "uvicorn[standard]>=0.37.0,<0.38.0",
    "starlette>=0.48.0,<0.49.0",
    "pydantic>=2.11.0,<3.0.0",
    "pydantic-settings>=2.10.0,<3.0.0",
    "loguru>=0.7.0,<0.8.0",
)
DEFAULT_REQUIRES_TXT = (
    "fastapi<0.118.0,>=0.117.1\n"
    "uvicorn[standard]<0.38.0,>=0.37.0\n"
    "starlette<0.49.0,>=0.48.0\n"
    "pydantic<3.0.0,>=2.11.0\n"
    "pydantic-settings<3.0.0,>=2.10.0\n"
    "loguru<0.8.0,>=0.7.0\n"
)


def test_httpx_dependency_range_is_consistent_for_testclient_users() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    optional_dependencies = pyproject["project"]["optional-dependencies"]

    assert "httpx>=0.27.0,<0.29.0" in optional_dependencies["http"]
    assert "httpx>=0.27.0,<0.29.0" in optional_dependencies["dev"]


def test_release_checker_dependencies_are_declared_for_dev_workflows() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    optional_dependencies = pyproject["project"]["optional-dependencies"]

    assert "packaging>=24.0" in optional_dependencies["dev"]


def test_plugin_manifest_recommended_extras_are_defined_package_extras() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    optional_dependencies = pyproject["project"]["optional-dependencies"]
    manifest = PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins()).manifest()

    recommended_extras = {
        extra
        for plugin in manifest.values()
        for extra in cast(list[str], plugin["recommended_extras"])
    }

    assert recommended_extras <= set(optional_dependencies)


def test_source_distribution_manifest_prunes_non_package_roots() -> None:
    manifest = Path("MANIFEST.in").read_text(encoding="utf-8")

    for root in (".github", "build", "dist", "docs", "examples", "scripts", "tests"):
        assert f"prune {root}" in manifest


def test_ci_package_job_runs_generated_project_smoke() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "python scripts/verify_local.py --skip-core --package --smoke" in workflow
    assert "python -m build" not in workflow
    assert "python -m twine check dist/*" not in workflow
    assert "python scripts/check_distribution.py dist/*" not in workflow
    assert "Smoke test generated project profiles" not in workflow
    assert "Smoke test plugin templates" not in workflow
    assert "Smoke test observability extra" not in workflow
    assert "scripts/smoke_generated_projects.py" not in workflow
    assert "scripts/smoke_plugin_templates.py" not in workflow
    assert ".venv-generated" not in workflow
    assert ".venv-observability" not in workflow
    assert "${WHEEL}[dev]" not in workflow


def test_ci_test_job_uses_shared_local_verification_script() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "python scripts/verify_local.py" in workflow
    assert "python -m black --check infra tests scripts" not in workflow
    assert "python scripts/typecheck.py" not in workflow
    assert "pytest -v" not in workflow


def test_typecheck_script_covers_release_and_scaffold_contracts() -> None:
    module = _load_script("scripts/typecheck.py")

    assert module.CHECK_PATHS == ("infra", "scripts", "tests")


def test_distribution_check_accepts_clean_artifacts(tmp_path) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 0


def test_distribution_check_rejects_unexpected_pyproject_build_backend(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[build-system]\n"
            'requires = ["setuptools>=77.0.0", "wheel"]\n'
            'build-backend = "hatchling.build"\n\n'
            + _pyproject_with_core_metadata(
                'name = "fastapi-infra"',
                'version = "0.2.0"',
                'requires-python = ">=3.11"',
            )
        ),
        "pyproject unexpected build-backend 'hatchling.build'",
    )


def test_distribution_check_rejects_unexpected_pyproject_build_requires(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[build-system]\n"
            'requires = ["setuptools>=77.0.0", "wheel", "hatchling"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            + _pyproject_with_core_metadata(
                'name = "fastapi-infra"',
                'version = "0.2.0"',
                'requires-python = ">=3.11"',
            )
        ),
        "pyproject unexpected build-system.requires ['setuptools>=77.0.0', 'wheel', 'hatchling']",
    )


def test_distribution_check_rejects_unsupported_pyproject_build_system_field(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[build-system]\n"
            'requires = ["setuptools>=77.0.0", "wheel"]\n'
            'build-backend = "setuptools.build_meta"\n'
            'backend-path = ["."]\n\n'
            + _pyproject_with_core_metadata(
                'name = "fastapi-infra"',
                'version = "0.2.0"',
                'requires-python = ">=3.11"',
            )
        ),
        "pyproject unsupported build-system field backend-path",
    )


def _assert_distribution_check_rejects_pyproject(
    tmp_path: Path,
    capsys: Any,
    pyproject: str,
    expected_error: str,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=pyproject,
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert expected_error in captured.err


def _pyproject_with_core_metadata(
    *project_lines: str,
    scripts: str | None = 'fastapi-infra = "infra.cli:main"',
) -> str:
    content = (
        "[project]\n" + "\n".join(project_lines) + "\n"
        "dependencies = [\n"
        + "".join(f'    "{dependency}",\n' for dependency in DEFAULT_METADATA_DEPENDENCIES)
        + "]\n"
    )
    if scripts is not None:
        content += "\n[project.scripts]\n" + scripts + "\n"
    return content


def test_distribution_check_accepts_sdist_root_directory_entry(tmp_path) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        include_root_dir_entry=True,
    )

    assert module.main([str(wheel), str(source)]) == 0


def test_distribution_check_accepts_setuptools_sdist_metadata_files(tmp_path) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=(
            "fastapi_infra.egg-info/PKG-INFO",
            "fastapi_infra.egg-info/SOURCES.txt",
            "fastapi_infra.egg-info/dependency_links.txt",
            "fastapi_infra.egg-info/entry_points.txt",
            "fastapi_infra.egg-info/requires.txt",
            "fastapi_infra.egg-info/top_level.txt",
            "setup.cfg",
        ),
    )

    assert module.main([str(wheel), str(source)]) == 0


def test_distribution_check_accepts_wheel_directory_entries(tmp_path) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        directory_entries=("infra", "fastapi_infra-0.2.0.dist-info"),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 0


def test_distribution_check_rejects_invalid_utf8_wheel_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=b"\xff",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid UTF-8 text in fastapi_infra-0.2.0.dist-info/METADATA" in captured.err


def test_distribution_check_rejects_invalid_utf8_sdist_pkg_info(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=b"\xff",
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid UTF-8 text in fastapi_infra-0.2.0/PKG-INFO" in captured.err


def test_distribution_check_rejects_invalid_utf8_sdist_sources_txt(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/SOURCES.txt",),
        extra_file_contents={"fastapi_infra.egg-info/SOURCES.txt": b"\xff"},
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid UTF-8 text in fastapi_infra.egg-info/SOURCES.txt" in captured.err


def test_distribution_check_rejects_duplicate_wheel_archive_member(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("infra/cli.py", "")
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "duplicate archive entry infra/cli.py" in captured.err


def test_distribution_check_rejects_parent_traversal_archive_member(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_extra="../evil.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n",
    )
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../evil.py", "")
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "unsafe archive entry ../evil.py" in captured.err


def test_distribution_check_rejects_backslash_archive_member(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_extra="infra\\evil.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n",
    )
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("infra\\evil.py", "")
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "unsafe archive entry infra\\evil.py" in captured.err


def test_distribution_check_rejects_empty_path_segment_archive_member(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_extra="infra//evil.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n",
    )
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("infra//evil.py", "")
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "unsafe archive entry infra//evil.py" in captured.err


def test_distribution_check_rejects_wheel_generated_cache_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        extra_files=("infra/__pycache__/cli.cpython-311.pyc",),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "generated cache file infra/__pycache__/cli.cpython-311.pyc" in captured.err


def test_distribution_check_rejects_sdist_generated_cache_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("infra/__pycache__/cli.cpython-311.pyc",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "generated cache file infra/__pycache__/cli.cpython-311.pyc" in captured.err


def test_distribution_check_rejects_wheel_generated_metadata_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        extra_files=(".DS_Store",),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "generated metadata file .DS_Store" in captured.err


def test_distribution_check_rejects_sdist_generated_metadata_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("._README.md",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "generated metadata file ._README.md" in captured.err


def test_distribution_check_rejects_unexpected_wheel_top_level_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        extra_files=("evil.py",),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel contains unexpected top-level path evil.py" in captured.err


def test_distribution_check_rejects_unexpected_sdist_top_level_path(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("other_package/__init__.py",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist contains unexpected top-level path other_package/__init__.py" in captured.err


def test_distribution_check_rejects_unexpected_sdist_egg_info_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/evil.py",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist contains unexpected egg-info file fastapi_infra.egg-info/evil.py" in (
        captured.err
    )


def test_distribution_check_rejects_sdist_egg_info_metadata_directory(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/PKG-INFO",),
        directory_entries=("fastapi_infra.egg-info/PKG-INFO",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "sdist metadata file is not a regular file fastapi_infra.egg-info/PKG-INFO" in captured.err
    )


def test_distribution_check_rejects_sdist_top_level_metadata_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/top_level.txt",),
        extra_file_contents={"fastapi_infra.egg-info/top_level.txt": "infra\nother\n"},
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist top_level.txt unexpected top-level package other" in captured.err


def test_distribution_check_rejects_duplicate_sdist_top_level_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/top_level.txt",),
        extra_file_contents={"fastapi_infra.egg-info/top_level.txt": "infra\ninfra\n"},
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist top_level.txt duplicate top-level package infra" in captured.err


def test_distribution_check_rejects_sdist_entry_points_metadata_drift(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/entry_points.txt",),
        extra_file_contents={"fastapi_infra.egg-info/entry_points.txt": ""},
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist entry_points.txt missing console script fastapi-infra = infra.cli:main" in (
        captured.err
    )


def test_distribution_check_rejects_unexpected_sdist_console_entry_point(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/entry_points.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/entry_points.txt": (
                "[console_scripts]\n"
                "fastapi-infra = infra.cli:main\n"
                "fastapi-infra-dev = infra.dev:main\n"
            )
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "sdist entry_points.txt unexpected console script " "fastapi-infra-dev = infra.dev:main"
    ) in captured.err


def test_distribution_check_rejects_case_mismatched_sdist_console_entry_point(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/entry_points.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/entry_points.txt": (
                "[console_scripts]\n" "FastAPI-Infra = infra.cli:main\n"
            )
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist entry_points.txt missing console script fastapi-infra = infra.cli:main" in (
        captured.err
    )


def test_distribution_check_rejects_unexpected_sdist_entry_point_section(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/entry_points.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/entry_points.txt": (
                "[console_scripts]\n"
                "fastapi-infra = infra.cli:main\n"
                "\n"
                "[gui_scripts]\n"
                "fastapi-infra-gui = infra.gui:main\n"
            )
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist entry_points.txt unexpected entry point section gui_scripts" in captured.err


def test_distribution_check_rejects_sdist_egg_info_pkg_info_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/PKG-INFO",),
        extra_file_contents={
            "fastapi_infra.egg-info/PKG-INFO": "Name: fastapi-infra\nVersion: 9.9.9\n"
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist egg-info PKG-INFO does not match root PKG-INFO" in captured.err


def test_distribution_check_rejects_sdist_sources_referencing_missing_file(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/SOURCES.txt",),
        extra_file_contents={"fastapi_infra.egg-info/SOURCES.txt": "infra/missing.py\n"},
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist SOURCES.txt references missing archive entry infra/missing.py" in captured.err


def test_distribution_check_rejects_duplicate_sdist_sources_entry(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/SOURCES.txt",),
        extra_file_contents={"fastapi_infra.egg-info/SOURCES.txt": "infra/cli.py\ninfra/cli.py\n"},
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist SOURCES.txt duplicate entry infra/cli.py" in captured.err


def test_distribution_check_rejects_sdist_sources_missing_archive_file(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/SOURCES.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/SOURCES.txt": (
                "infra/__init__.py\n"
                "infra/scaffold.py\n"
                "infra/provider_tests/test_live_providers.py\n"
                "PKG-INFO\n"
                "LICENSE\n"
                "MANIFEST.in\n"
                "README.md\n"
                "pyproject.toml\n"
                "fastapi_infra.egg-info/SOURCES.txt\n"
            )
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist SOURCES.txt missing archive entry infra/cli.py" in captured.err


def test_distribution_check_accepts_sdist_sources_without_generated_pkg_info(tmp_path) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/SOURCES.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/SOURCES.txt": (
                "infra/__init__.py\n"
                "infra/cli.py\n"
                "infra/scaffold.py\n"
                "infra/provider_tests/test_live_providers.py\n"
                "LICENSE\n"
                "MANIFEST.in\n"
                "README.md\n"
                "pyproject.toml\n"
                "fastapi_infra.egg-info/SOURCES.txt\n"
            )
        },
    )

    assert module.main([str(wheel), str(source)]) == 0


def test_distribution_check_rejects_sdist_requires_metadata_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/requires.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/requires.txt": (
                "fastapi<0.118.0,>=0.117.1\n"
                "uvicorn[standard]<0.38.0,>=0.37.0\n"
                "starlette<0.49.0,>=0.48.0\n"
                "pydantic<3.0.0,>=2.11.0\n"
                "pydantic-settings<3.0.0,>=2.10.0\n"
            )
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist requires.txt missing required dependency loguru" in captured.err


def test_distribution_check_rejects_invalid_sdist_requires_dependency(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/requires.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/requires.txt": (
                _requires_txt_content() + "not a valid requirement !!!\n"
            )
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid sdist requires.txt dependency not a valid requirement !!!" in captured.err


def test_distribution_check_rejects_invalid_sdist_requires_section(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/requires.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/requires.txt": _requires_txt_content() + "[]\npytest\n"
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid sdist requires.txt section []" in captured.err


def test_distribution_check_rejects_sdist_dependency_links_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        extra_files=("fastapi_infra.egg-info/dependency_links.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/dependency_links.txt": "https://packages.example.test\n"
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist dependency_links.txt must be empty" in captured.err


def test_distribution_check_rejects_top_level_test_artifacts(tmp_path) -> None:
    module = _load_script("scripts/check_distribution.py")
    source = tmp_path / "fastapi_infra-0.2.0.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        for entry in [
            "infra/__init__.py",
            "infra/cli.py",
            "infra/scaffold.py",
            "infra/provider_tests/test_live_providers.py",
            "LICENSE",
            "MANIFEST.in",
            "README.md",
            "pyproject.toml",
            "tests/test_no_business_imports.py",
        ]:
            path = tmp_path / entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
            archive.add(path, arcname=f"fastapi_infra-0.2.0/{entry}")

    assert module.main([str(source)]) == 1


def test_distribution_check_requires_wheel_and_sdist(tmp_path) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")

    assert module.main([str(wheel)]) == 1


def test_distribution_check_rejects_duplicate_wheels(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    first_wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    second_wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.1-py3-none-any.whl")
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(first_wheel), str(second_wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "expected exactly one wheel artifact (*.whl); found 2" in captured.err


def test_distribution_check_rejects_mismatched_artifact_versions(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.1.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "artifact name/version mismatch: wheel fastapi-infra 0.2.0, " "sdist fastapi-infra 0.2.1"
    ) in captured.err


def test_distribution_check_rejects_wheel_metadata_mismatch(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_version="0.2.1",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "wheel metadata mismatch: filename fastapi-infra 0.2.0, " "METADATA fastapi-infra 0.2.1"
    ) in captured.err


def test_distribution_check_rejects_missing_wheel_required_dependency(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA missing required dependency loguru" in captured.err


def test_distribution_check_rejects_wheel_dependency_constraint_mismatch(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA dependency mismatch for fastapi" in captured.err


def test_distribution_check_rejects_unexpected_wheel_required_dependency(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "requests>=2.0.0",
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA unexpected required dependency requests" in captured.err


def test_distribution_check_rejects_invalid_wheel_requires_dist(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "not a valid requirement !!!",
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "wheel METADATA invalid Requires-Dist dependency not a valid requirement !!!"
        in captured.err
    )


def test_distribution_check_rejects_direct_url_dependency(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    direct_url_dependency = "fastapi @ https://example.test/fastapi-0.117.1-py3-none-any.whl"
    dependencies = (
        direct_url_dependency,
        "uvicorn[standard]>=0.37.0,<0.38.0",
        "starlette>=0.48.0,<0.49.0",
        "pydantic>=2.11.0,<3.0.0",
        "pydantic-settings>=2.10.0,<3.0.0",
        "loguru>=0.7.0,<0.8.0",
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=dependencies,
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=dependencies,
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            f'    "{direct_url_dependency}",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        extra_files=("fastapi_infra.egg-info/requires.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/requires.txt": "\n".join(dependencies) + "\n",
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject direct URL dependency fastapi" in captured.err
    assert "wheel METADATA direct URL dependency fastapi" in captured.err
    assert "sdist PKG-INFO direct URL dependency fastapi" in captured.err
    assert "sdist requires.txt direct URL dependency fastapi" in captured.err


def test_distribution_check_rejects_direct_url_optional_dependency(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    direct_url_dependency = "pytest @ https://example.test/pytest-8.0.0-py3-none-any.whl"
    optional_dependency = f'{direct_url_dependency} ; extra == "dev"'
    dependencies = (
        "fastapi>=0.117.1,<0.118.0",
        "uvicorn[standard]>=0.37.0,<0.38.0",
        "starlette>=0.48.0,<0.49.0",
        "pydantic>=2.11.0,<3.0.0",
        "pydantic-settings>=2.10.0,<3.0.0",
        "loguru>=0.7.0,<0.8.0",
        optional_dependency,
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=dependencies,
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=dependencies,
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            "dev = [\n"
            f'    "{direct_url_dependency}",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        extra_files=("fastapi_infra.egg-info/requires.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/requires.txt": (
                "fastapi>=0.117.1,<0.118.0\n"
                "uvicorn[standard]>=0.37.0,<0.38.0\n"
                "starlette>=0.48.0,<0.49.0\n"
                "pydantic>=2.11.0,<3.0.0\n"
                "pydantic-settings>=2.10.0,<3.0.0\n"
                "loguru>=0.7.0,<0.8.0\n"
                "\n"
                "[dev]\n"
                f"{direct_url_dependency}\n"
            ),
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject direct URL optional dependency dev:pytest" in captured.err
    assert "wheel METADATA direct URL optional dependency dev:pytest" in captured.err
    assert "sdist PKG-INFO direct URL optional dependency dev:pytest" in captured.err
    assert "sdist requires.txt direct URL optional dependency dev:pytest" in captured.err


def test_distribution_check_rejects_duplicate_wheel_required_dependency(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate required dependency fastapi" in captured.err


def test_distribution_check_rejects_duplicate_pyproject_required_dependency(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject duplicate required dependency fastapi" in captured.err


def test_distribution_check_rejects_invalid_pyproject_required_dependency(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            '    "not a valid requirement !!!",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid required dependency not a valid requirement !!!" in captured.err


def test_distribution_check_rejects_pyproject_required_dependency_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    " fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid required dependency  fastapi>=0.117.1,<0.118.0" in captured.err


def test_distribution_check_rejects_non_string_pyproject_required_dependency(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "    123,\n"
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid required dependency 123" in captured.err


def test_distribution_check_rejects_missing_pyproject_requires_python(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist pyproject.toml missing project.requires-python" in captured.err


def test_distribution_check_rejects_non_string_pyproject_requires_python(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            "requires-python = 123",
        ),
        "pyproject invalid requires-python 123",
    )


def test_distribution_check_rejects_invalid_pyproject_requires_python(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'requires-python = "not a specifier"',
        ),
        "pyproject invalid requires-python 'not a specifier'",
    )


def test_distribution_check_rejects_pyproject_requires_python_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'requires-python = " >=3.11"',
        ),
        "pyproject invalid requires-python ' >=3.11'",
    )


def test_distribution_check_rejects_missing_pyproject_dependencies(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist pyproject.toml missing project.dependencies list" in captured.err


def test_distribution_check_rejects_non_list_pyproject_dependencies(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            'dependencies = "fastapi>=0.117.1,<0.118.0"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        "pyproject invalid dependencies list 'fastapi>=0.117.1,<0.118.0'",
    )


def test_distribution_check_rejects_non_string_pyproject_name(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            "name = 123",
            'version = "0.2.0"',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid project.name 123",
    )


def test_distribution_check_rejects_non_string_pyproject_version(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            "version = 123",
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid project.version 123",
    )


def test_distribution_check_rejects_invalid_pyproject_name(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi infra"',
            'version = "0.2.0"',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid project.name 'fastapi infra'",
    )


def test_distribution_check_rejects_invalid_pyproject_version(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "not a version"',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid project.version 'not a version'",
    )


def test_distribution_check_rejects_pyproject_version_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = " 0.2.0"',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid project.version ' 0.2.0'",
    )


def test_distribution_check_rejects_missing_pyproject_core_dependency(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    dependencies_without_fastapi = (
        "uvicorn[standard]>=0.37.0,<0.38.0",
        "starlette>=0.48.0,<0.49.0",
        "pydantic>=2.11.0,<3.0.0",
        "pydantic-settings>=2.10.0,<3.0.0",
        "loguru>=0.7.0,<0.8.0",
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=dependencies_without_fastapi,
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=dependencies_without_fastapi,
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject missing required core dependency fastapi" in captured.err


def test_distribution_check_rejects_missing_wheel_provides_extra(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=8.0.0"]\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA missing provided extra dev" in captured.err


def test_distribution_check_rejects_unexpected_wheel_provides_extra(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_extras=("docs",),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA unexpected provided extra docs" in captured.err


def test_distribution_check_rejects_invalid_wheel_provides_extra(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_extras=("-dev",),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA invalid provided extra -dev" in captured.err


def test_distribution_check_rejects_invalid_wheel_requires_dist_extra_marker(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "pytest>=8.0.0; extra == '-dev'",
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA invalid Requires-Dist extra marker -dev" in captured.err


def test_distribution_check_rejects_wheel_requires_dist_undeclared_extra(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "pytest>=8.0.0; extra == 'dev'",
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA Requires-Dist references undeclared extra dev" in captured.err


def test_distribution_check_rejects_duplicate_wheel_provides_extra(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "pytest>=8.0.0; extra == 'dev'",
        ),
        metadata_extras=("dev", "dev"),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
                "pytest>=8.0.0; extra == 'dev'",
            ),
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=8.0.0"]\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate provided extra dev" in captured.err


def test_distribution_check_rejects_duplicate_normalized_pyproject_extra(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_extras=("dev-test",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=("dev-test",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            "dev_test = []\n"
            "dev-test = []\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject duplicate optional dependency extra dev-test" in captured.err


def test_distribution_check_rejects_invalid_pyproject_extra_name(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_extras=("-dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=("-dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            '"-dev" = []\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid optional dependency extra -dev" in captured.err


def test_distribution_check_rejects_missing_wheel_extra_dependency(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
                "pytest>=8.0.0; extra == 'dev'",
            ),
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=8.0.0"]\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA missing optional dependency dev:pytest" in captured.err


def test_distribution_check_rejects_unexpected_wheel_extra_dependency(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "pytest>=8.0.0; extra == 'dev'",
            "requests>=2.0.0; extra == 'dev'",
        ),
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
                "pytest>=8.0.0; extra == 'dev'",
            ),
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=8.0.0"]\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA unexpected optional dependency dev:requests" in captured.err


def test_distribution_check_rejects_duplicate_wheel_extra_dependency(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "pytest>=8.0.0; extra == 'dev'",
            "pytest>=8.0.0; extra == 'dev'",
        ),
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
                "pytest>=8.0.0; extra == 'dev'",
            ),
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=8.0.0"]\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate optional dependency dev:pytest" in captured.err


def test_distribution_check_rejects_duplicate_pyproject_extra_dependency(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "pytest>=8.0.0; extra == 'dev'",
        ),
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
                "pytest>=8.0.0; extra == 'dev'",
            ),
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=8.0.0", "pytest>=8.0.0"]\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject duplicate optional dependency dev:pytest" in captured.err


def test_distribution_check_rejects_invalid_pyproject_extra_dependency(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = ["not a valid requirement !!!"]\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid optional dependency dev:not a valid requirement !!!" in captured.err


def test_distribution_check_rejects_pyproject_extra_dependency_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    dependencies = (
        "fastapi>=0.117.1,<0.118.0",
        "uvicorn[standard]>=0.37.0,<0.38.0",
        "starlette>=0.48.0,<0.49.0",
        "pydantic>=2.11.0,<3.0.0",
        "pydantic-settings>=2.10.0,<3.0.0",
        "loguru>=0.7.0,<0.8.0",
        'pytest>=8.0.0 ; extra == "dev"',
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=dependencies,
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=dependencies,
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = [" pytest>=8.0.0"]\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        extra_files=("fastapi_infra.egg-info/requires.txt",),
        extra_file_contents={
            "fastapi_infra.egg-info/requires.txt": (
                "fastapi>=0.117.1,<0.118.0\n"
                "uvicorn[standard]>=0.37.0,<0.38.0\n"
                "starlette>=0.48.0,<0.49.0\n"
                "pydantic>=2.11.0,<3.0.0\n"
                "pydantic-settings>=2.10.0,<3.0.0\n"
                "loguru>=0.7.0,<0.8.0\n"
                "\n"
                "[dev]\n"
                "pytest>=8.0.0\n"
            ),
        },
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid optional dependency dev: pytest>=8.0.0" in captured.err


def test_distribution_check_rejects_non_string_pyproject_extra_dependency(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            "dev = [123]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid optional dependency dev:123" in captured.err


def test_distribution_check_rejects_non_list_pyproject_extra_dependencies(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = "pytest>=8.0.0"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid optional dependency list dev" in captured.err


def test_distribution_check_rejects_non_table_pyproject_optional_dependencies(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            'optional-dependencies = "dev"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid optional dependencies table" in captured.err


def test_distribution_check_rejects_wheel_extra_dependency_marker_mismatch(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "pytest>=8.0.0; extra == 'dev'",
        ),
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=_pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
                "pytest>=8.0.0; python_version < '3.13' and extra == 'dev'",
            ),
            extras=("dev",),
        ),
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            "dev = ['pytest>=8.0.0; python_version < \"3.13\"']\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA optional dependency mismatch for dev:pytest" in captured.err


def test_distribution_check_rejects_sdist_pkg_info_metadata_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_extras=("dev",),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info="Name: fastapi-infra\nVersion: 0.2.0\n",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=8.0.0"]\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist PKG-INFO Requires-Python mismatch: pyproject >=3.11, PKG-INFO <missing>" in (
        captured.err
    )
    assert "sdist PKG-INFO missing required dependency loguru" in captured.err
    assert "sdist PKG-INFO missing provided extra dev" in captured.err


def test_distribution_check_rejects_invalid_sdist_pkg_info_requires_dist(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    pkg_info = _pkg_info_content(
        name="fastapi-infra",
        version="0.2.0",
        requires_python=">=3.11",
        dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
            "not a valid requirement !!!",
        ),
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "sdist PKG-INFO invalid Requires-Dist dependency not a valid requirement !!!"
        in captured.err
    )


def test_distribution_check_rejects_wheel_requires_python_mismatch(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_requires_python=">=3.12",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "wheel METADATA Requires-Python mismatch: pyproject >=3.11, METADATA >=3.12" in captured.err
    )


def test_distribution_check_rejects_missing_wheel_metadata_version(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=_wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        ).replace("Metadata-Version: 2.4\n", ""),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA missing Metadata-Version" in captured.err


def test_distribution_check_rejects_license_expression_with_old_metadata_version(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        ).replace("Metadata-Version: 2.4\n", "Metadata-Version: 2.3\n")
        + "License-Expression: MIT\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA License-Expression requires Metadata-Version 2.4" in captured.err


def test_distribution_check_rejects_dynamic_metadata_with_old_metadata_version(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        ).replace("Metadata-Version: 2.4\n", "Metadata-Version: 2.1\n")
        + "Dynamic: license-file\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA Dynamic requires Metadata-Version 2.2" in captured.err


def test_distribution_check_rejects_pyproject_dynamic_metadata_field(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'dynamic = ["version"]',
            'requires-python = ">=3.11"',
        ),
        "pyproject unexpected dynamic metadata field version",
    )


def test_distribution_check_rejects_pyproject_dynamic_metadata_field_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'dynamic = [" version"]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid dynamic metadata field ' version'",
    )


def test_distribution_check_rejects_wheel_dynamic_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Dynamic: Version\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA unexpected Dynamic metadata field Version" in captured.err


def test_distribution_check_rejects_wheel_provides_dist_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Provides-Dist: fastapi-infra-legacy\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "wheel METADATA unsupported Provides-Dist metadata field fastapi-infra-legacy"
        in captured.err
    )


def test_distribution_check_rejects_wheel_legacy_provides_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Provides: fastapi-infra-legacy\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA unsupported Provides metadata field fastapi-infra-legacy" in (
        captured.err
    )


def test_distribution_check_rejects_wheel_home_page_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Home-page: https://legacy.example.test\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA unsupported Home-page metadata field https://legacy.example.test" in (
        captured.err
    )


def test_distribution_check_rejects_wheel_maintainer_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Maintainer: Release Team\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA unsupported Maintainer metadata field Release Team" in captured.err


def test_distribution_check_rejects_pyproject_maintainers_field(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'requires-python = ">=3.11"',
            'maintainers = [{ name = "Release Team" }]',
        ),
        "pyproject unsupported metadata field maintainers",
    )


def test_distribution_check_rejects_pyproject_gui_scripts_field(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            _pyproject_with_core_metadata(
                'name = "fastapi-infra"',
                'version = "0.2.0"',
                'requires-python = ">=3.11"',
            )
            + "\n[project.gui-scripts]\n"
            + 'fastapi-infra-gui = "infra.gui:main"\n'
        ),
        "pyproject unsupported metadata field gui-scripts",
    )


def test_distribution_check_rejects_pyproject_entry_points_field(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            _pyproject_with_core_metadata(
                'name = "fastapi-infra"',
                'version = "0.2.0"',
                'requires-python = ">=3.11"',
            )
            + "\n[project.entry-points.fastapi_infra]\n"
            + 'dev = "infra.dev:main"\n'
        ),
        "pyproject unsupported metadata field entry-points",
    )


def test_distribution_check_rejects_wheel_summary_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    pkg_info = _pkg_info_content(
        name="fastapi-infra",
        version="0.2.0",
        requires_python=">=3.11",
        dependencies=(
            "fastapi>=0.117.1,<0.118.0",
            "uvicorn[standard]>=0.37.0,<0.38.0",
            "starlette>=0.48.0,<0.49.0",
            "pydantic>=2.11.0,<3.0.0",
            "pydantic-settings>=2.10.0,<3.0.0",
            "loguru>=0.7.0,<0.8.0",
        ),
    ).replace("Version: 0.2.0\n", "Version: 0.2.0\nSummary: Stable release package\n")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'description = "Stable release package"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA Summary mismatch" in captured.err


def test_distribution_check_rejects_non_string_pyproject_description(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "description = 123\n"
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        "pyproject invalid description 123",
    )


def test_distribution_check_rejects_pyproject_description_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'description = " Stable release package"',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid description ' Stable release package'",
    )


def test_distribution_check_rejects_wheel_project_url_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Project-URL: Homepage, https://example.test\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.urls]\n"
            'Homepage = "https://example.test"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA missing project URL Homepage" in captured.err


def test_distribution_check_rejects_non_string_pyproject_url(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.urls]\n"
            "Homepage = 123\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid project URL Homepage:123" in captured.err


def test_distribution_check_rejects_invalid_pyproject_url(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Project-URL: Homepage, not-a-url\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Project-URL: Homepage, not-a-url\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.urls]\n"
            'Homepage = "not-a-url"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid project URL Homepage:'not-a-url'" in captured.err
    assert "wheel METADATA invalid project URL metadata 'Homepage, not-a-url'" in captured.err
    assert "sdist PKG-INFO invalid project URL metadata 'Homepage, not-a-url'" in captured.err


def test_distribution_check_rejects_project_url_with_raw_whitespace(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    url = "https://example.test/docs path"
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + f"Project-URL: Documentation, {url}\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + f"Project-URL: Documentation, {url}\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.urls]\n"
            f'Documentation = "{url}"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert f"pyproject invalid project URL Documentation:{url!r}" in captured.err
    assert f"wheel METADATA invalid project URL metadata 'Documentation, {url}'" in captured.err
    assert f"sdist PKG-INFO invalid project URL metadata 'Documentation, {url}'" in captured.err


def test_distribution_check_rejects_too_long_project_url_label(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    long_label = "DocumentationMirrorLinkThatIsTooLong"
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + f"Project-URL: {long_label}, https://example.test\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + f"Project-URL: {long_label}, https://example.test\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.urls]\n"
            f'{long_label} = "https://example.test"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert f"pyproject invalid project URL label {long_label!r}" in captured.err
    assert f"wheel METADATA invalid project URL label metadata {long_label!r}" in captured.err
    assert f"sdist PKG-INFO invalid project URL label metadata {long_label!r}" in captured.err


def test_distribution_check_rejects_project_url_label_with_comma(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    label = "Docs,Mirror"
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + f"Project-URL: {label}, https://example.test\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + f"Project-URL: {label}, https://example.test\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.urls]\n"
            f'"{label}" = "https://example.test"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert f"pyproject invalid project URL label {label!r}" in captured.err
    assert f"wheel METADATA invalid project URL label metadata {label!r}" in captured.err
    assert f"sdist PKG-INFO invalid project URL label metadata {label!r}" in captured.err


def test_distribution_check_rejects_malformed_wheel_project_url_metadata(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Project-URL: Homepage https://example.test\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA malformed project URL metadata 'Homepage https://example.test'" in (
        captured.err
    )


def test_distribution_check_rejects_duplicate_wheel_project_url_metadata_without_pyproject(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Project-URL: Homepage, https://example.test\n"
        + "Project-URL: Homepage, https://mirror.example.test\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate project URL Homepage" in captured.err


def test_distribution_check_rejects_case_variant_project_url_labels(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Project-URL: Homepage, https://example.test\n"
        + "Project-URL: homepage, https://mirror.example.test\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Project-URL: Homepage, https://example.test\n"
        + "Project-URL: homepage, https://mirror.example.test\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.urls]\n"
            'Homepage = "https://example.test"\n'
            'homepage = "https://mirror.example.test"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject duplicate project URL label homepage" in captured.err
    assert "wheel METADATA duplicate project URL label homepage" in captured.err
    assert "sdist PKG-INFO duplicate project URL label homepage" in captured.err


def test_distribution_check_rejects_wheel_license_expression_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "License-Expression: MIT\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'license = "MIT"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA License-Expression mismatch" in captured.err


def test_distribution_check_rejects_invalid_wheel_license_expression_without_pyproject(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "License-Expression: not a license\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA invalid License-Expression 'not a license'" in captured.err


def test_distribution_check_rejects_non_string_pyproject_license(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "license = 123\n"
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        "pyproject invalid license 123",
    )


def test_distribution_check_rejects_invalid_pyproject_license_expression(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license = "not a license"',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid license expression 'not a license'",
    )


def test_distribution_check_rejects_pyproject_license_expression_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license = " MIT"',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid license expression ' MIT'",
    )


def test_distribution_check_rejects_wheel_keywords_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Keywords: fastapi,infrastructure\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'keywords = ["fastapi", "infrastructure"]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA Keywords mismatch" in captured.err


def test_distribution_check_rejects_non_string_pyproject_keyword(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "keywords = [123]\n"
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid keyword 123" in captured.err


def test_distribution_check_rejects_pyproject_keyword_with_comma(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'keywords = ["fastapi,infra"]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid keyword 'fastapi,infra'",
    )


def test_distribution_check_rejects_non_list_pyproject_keywords(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'keywords = "fastapi"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        "pyproject invalid keywords list 'fastapi'",
    )


def test_distribution_check_rejects_duplicate_pyproject_keyword(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Keywords: fastapi, fastapi\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Keywords: fastapi, fastapi\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'keywords = ["fastapi", "fastapi"]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject duplicate keyword fastapi" in captured.err


def test_distribution_check_rejects_wheel_classifier_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Classifier: Framework :: FastAPI\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'classifiers = ["Framework :: FastAPI"]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA missing classifier Framework :: FastAPI" in captured.err


def test_distribution_check_rejects_non_string_pyproject_classifier(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "classifiers = [123]\n"
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid classifier 123" in captured.err


def test_distribution_check_rejects_blank_pyproject_classifier(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'classifiers = [""]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid classifier ''",
    )


def test_distribution_check_rejects_pyproject_classifier_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'classifiers = [" Framework :: FastAPI"]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid classifier ' Framework :: FastAPI'",
    )


def test_distribution_check_rejects_non_list_pyproject_classifiers(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'classifiers = "Framework :: FastAPI"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        "pyproject invalid classifiers list 'Framework :: FastAPI'",
    )


def test_distribution_check_rejects_duplicate_pyproject_classifier(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Classifier: Framework :: FastAPI\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Classifier: Framework :: FastAPI\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'classifiers = ["Framework :: FastAPI", "Framework :: FastAPI"]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject duplicate classifier Framework :: FastAPI" in captured.err


def test_distribution_check_rejects_duplicate_wheel_classifier_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Classifier: Framework :: FastAPI\n"
        + "Classifier: Framework :: FastAPI\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Classifier: Framework :: FastAPI\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'classifiers = ["Framework :: FastAPI"]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate classifier metadata Framework :: FastAPI" in captured.err


def test_distribution_check_rejects_duplicate_wheel_classifier_metadata_without_pyproject(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Classifier: Framework :: FastAPI\n"
        + "Classifier: Framework :: FastAPI\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate classifier metadata Framework :: FastAPI" in captured.err


def test_distribution_check_rejects_wheel_author_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Author: AIMidPlatform Team\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'authors = [{ name = "AIMidPlatform Team" }]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA Author mismatch" in captured.err


def test_distribution_check_rejects_missing_author_email_metadata(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Author: AIMidPlatform Team\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Author: AIMidPlatform Team\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'authors = [{ name = "AIMidPlatform Team", email = "team@example.test" }]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA Author-email mismatch" in captured.err
    assert "sdist PKG-INFO Author-email mismatch" in captured.err


def test_distribution_check_accepts_author_email_metadata(tmp_path) -> None:
    module = _load_script("scripts/check_distribution.py")
    author_email = "AIMidPlatform Team <team@example.test>"
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + f"Author-email: {author_email}\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + f"Author-email: {author_email}\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'authors = [{ name = "AIMidPlatform Team", email = "team@example.test" }]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 0


def test_distribution_check_rejects_non_table_pyproject_author(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "authors = [123]\n"
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid author 123" in captured.err


def test_distribution_check_rejects_empty_pyproject_author_table(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            "authors = [{}]",
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid author {}",
    )


def test_distribution_check_rejects_non_list_pyproject_authors(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'authors = "AIMidPlatform Team"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        "pyproject invalid authors list 'AIMidPlatform Team'",
    )


def test_distribution_check_rejects_non_string_pyproject_author_name(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "authors = [{ name = 123 }]\n"
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid author name 123" in captured.err


def test_distribution_check_rejects_blank_pyproject_author_name(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'authors = [{ name = "" }]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid author name ''",
    )


def test_distribution_check_rejects_pyproject_author_name_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'authors = [{ name = " AIMidPlatform Team" }]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid author name ' AIMidPlatform Team'",
    )


def test_distribution_check_rejects_unsupported_pyproject_author_field(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'authors = [{ name = "AIMidPlatform Team", url = "https://example.test" }]',
            'requires-python = ">=3.11"',
        ),
        "pyproject unsupported author field url",
    )


def test_distribution_check_rejects_non_string_pyproject_author_email(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "authors = [{ email = 123 }]\n"
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid author email 123" in captured.err


def test_distribution_check_rejects_blank_pyproject_author_email(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'authors = [{ email = "" }]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid author email ''",
    )


def test_distribution_check_rejects_malformed_pyproject_author_email(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'authors = [{ email = "not-email" }]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid author email 'not-email'",
    )


def test_distribution_check_rejects_duplicate_pyproject_author(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Author: AIMidPlatform Team, AIMidPlatform Team\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Author: AIMidPlatform Team, AIMidPlatform Team\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'authors = [{ name = "AIMidPlatform Team" }, { name = "AIMidPlatform Team" }]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject duplicate author AIMidPlatform Team" in captured.err


def test_distribution_check_rejects_non_table_pyproject_urls(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'urls = "https://example.test"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        "pyproject invalid project URLs table 'https://example.test'",
    )


def test_distribution_check_rejects_duplicate_wheel_author_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Author: AIMidPlatform Team\n"
        + "Author: Other Team\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Author: AIMidPlatform Team\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'authors = [{ name = "AIMidPlatform Team" }]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate Author metadata field" in captured.err


def test_distribution_check_rejects_duplicate_wheel_name_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Name: fastapi-infra\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate Name metadata field" in captured.err


def test_distribution_check_rejects_wheel_readme_content_type_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Description-Content-Type: text/markdown\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'readme = "README.md"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA Description-Content-Type mismatch" in captured.err


def test_distribution_check_rejects_non_string_pyproject_readme(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "readme = 123\n"
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid readme 123" in captured.err


def test_distribution_check_rejects_unknown_pyproject_readme_content_type(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'readme = "README.foo"',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid readme content type README.foo",
    )


def test_distribution_check_rejects_blank_pyproject_readme_path(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'readme = ""',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid readme path ''",
    )


def test_distribution_check_rejects_pyproject_readme_path_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'readme = " README.md"',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid readme path ' README.md'",
    )


def test_distribution_check_rejects_missing_pyproject_readme_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Description-Content-Type: text/markdown\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Description-Content-Type: text/markdown\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'readme = "docs/README.md"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist missing pyproject readme file docs/README.md" in captured.err


def test_distribution_check_rejects_unsafe_pyproject_readme_path(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "Description-Content-Type: text/markdown\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "Description-Content-Type: text/markdown\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'readme = "../README.md"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid readme path '../README.md'" in captured.err


def test_distribution_check_rejects_wheel_license_file_metadata_drift(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "License-File: LICENSE\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'license-files = ["LICENSE"]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA missing license file metadata LICENSE" in captured.err


def test_distribution_check_rejects_non_string_pyproject_license_file(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            "license-files = [123]\n"
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject invalid license file 123" in captured.err


def test_distribution_check_rejects_unsafe_pyproject_license_file_path(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license-files = ["../LICENSE"]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid license file path '../LICENSE'",
    )


def test_distribution_check_rejects_blank_pyproject_license_file_path(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license-files = [""]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid license file path ''",
    )


def test_distribution_check_rejects_pyproject_license_file_path_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license-files = [" LICENSE"]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid license file path ' LICENSE'",
    )


def test_distribution_check_rejects_pyproject_license_file_path_with_trailing_slash(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license-files = ["LICENSE/"]',
            'requires-python = ">=3.11"',
        ),
        "pyproject invalid license file path 'LICENSE/'",
    )


def test_distribution_check_rejects_duplicate_pyproject_license_file(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "License-File: LICENSE\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "License-File: LICENSE\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=_pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license-files = ["LICENSE", "LICENSE"]',
            'requires-python = ">=3.11"',
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "pyproject duplicate license file LICENSE" in captured.err


def test_distribution_check_rejects_missing_sdist_pyproject_license_file(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=(
            _wheel_metadata_content(
                name="fastapi-infra",
                version="0.2.0",
                requires_python=">=3.11",
                dependencies=(
                    "fastapi>=0.117.1,<0.118.0",
                    "uvicorn[standard]>=0.37.0,<0.38.0",
                    "starlette>=0.48.0,<0.49.0",
                    "pydantic>=2.11.0,<3.0.0",
                    "pydantic-settings>=2.10.0,<3.0.0",
                    "loguru>=0.7.0,<0.8.0",
                ),
                extras=(),
            )
            + "License-File: NOTICE\n"
        ),
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "License-File: NOTICE\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=_pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license-files = ["NOTICE"]',
            'requires-python = ">=3.11"',
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist missing pyproject license file NOTICE" in captured.err


def test_distribution_check_rejects_non_list_pyproject_license_files(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        (
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'license-files = "LICENSE"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
        "pyproject invalid license files list 'LICENSE'",
    )


def test_distribution_check_rejects_duplicate_wheel_license_file_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "License-File: LICENSE\n"
        + "License-File: LICENSE\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "License-File: LICENSE\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra"\n'
            'version = "0.2.0"\n'
            'license-files = ["LICENSE"]\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "fastapi>=0.117.1,<0.118.0",\n'
            '    "uvicorn[standard]>=0.37.0,<0.38.0",\n'
            '    "starlette>=0.48.0,<0.49.0",\n'
            '    "pydantic>=2.11.0,<3.0.0",\n'
            '    "pydantic-settings>=2.10.0,<3.0.0",\n'
            '    "loguru>=0.7.0,<0.8.0",\n'
            "]\n"
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate license file metadata LICENSE" in captured.err


def test_distribution_check_rejects_duplicate_wheel_license_file_metadata_without_pyproject(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "License-File: LICENSE\n"
        + "License-File: LICENSE\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA duplicate license file metadata LICENSE" in captured.err


def test_distribution_check_rejects_unsafe_wheel_license_file_metadata_without_pyproject(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "License-File: ../LICENSE\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel METADATA unsafe license file metadata ../LICENSE" in captured.err


def test_distribution_check_rejects_wheel_dist_info_directory_mismatch(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        dist_info_dir="fastapi_infra-0.2.1.dist-info",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "wheel dist-info directory mismatch: filename fastapi-infra 0.2.0, "
        "dist-info fastapi-infra 0.2.1"
    ) in captured.err


def test_distribution_check_rejects_sdist_metadata_mismatch(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        metadata_name="fastapi-infra-other",
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "sdist metadata mismatch: filename fastapi-infra 0.2.0, "
        "PKG-INFO fastapi-infra-other 0.2.0"
    ) in captured.err


def test_distribution_check_rejects_sdist_root_directory_mismatch(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        root_dir="fastapi_infra_wrong-0.2.0",
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "sdist root directory mismatch: expected fastapi_infra-0.2.0, "
        "found fastapi_infra_wrong-0.2.0"
    ) in captured.err


def test_distribution_check_rejects_sdist_root_level_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        root_files=("LICENSE",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist member outside root directory fastapi_infra-0.2.0: LICENSE" in captured.err


def test_distribution_check_rejects_sdist_symlink_member(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        symlink_entries={"infra/cli.py": "/tmp/evil.py"},
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "source distribution contains non-regular file infra/cli.py" in captured.err


def test_distribution_check_rejects_sdist_root_symlink_member(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        root_symlink_target="/tmp/evil-root",
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "source distribution root is not a directory fastapi_infra-0.2.0" in captured.err


def test_distribution_check_rejects_wheel_symlink_member(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        symlink_entries={"infra/cli.py": "../evil.py"},
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel contains non-regular file infra/cli.py" in captured.err


def test_distribution_check_rejects_wheel_required_package_directory(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        directory_entries=("infra/cli.py",),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "required package file is not a regular file infra/cli.py" in captured.err


def test_distribution_check_rejects_wheel_metadata_directory(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        directory_entries=("fastapi_infra-0.2.0.dist-info/METADATA",),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "required wheel metadata file is not a regular file METADATA" in captured.err


def test_distribution_check_rejects_sdist_required_package_directory(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        directory_entries=("infra/cli.py",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "required package file is not a regular file infra/cli.py" in captured.err


def test_distribution_check_rejects_sdist_required_source_directory(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        directory_entries=("pyproject.toml",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "required source file is not a regular file pyproject.toml" in captured.err


def test_distribution_check_rejects_sdist_pkg_info_directory(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        directory_entries=("PKG-INFO",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "required source file is not a regular file PKG-INFO" in captured.err


def test_distribution_check_rejects_missing_console_entry_point(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        entry_points="",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "missing console script entry point fastapi-infra = infra.cli:main" in captured.err


def test_distribution_check_rejects_unexpected_console_entry_point(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        entry_points=(
            "[console_scripts]\n"
            "fastapi-infra = infra.cli:main\n"
            "fastapi-infra-dev = infra.dev:main\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "unexpected console script entry point fastapi-infra-dev = infra.dev:main" in captured.err
    )


def test_distribution_check_rejects_case_mismatched_console_entry_point(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        entry_points="[console_scripts]\nFastAPI-Infra = infra.cli:main\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "missing console script entry point fastapi-infra = infra.cli:main" in captured.err


def test_distribution_check_rejects_unexpected_wheel_entry_point_section(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        entry_points=(
            "[console_scripts]\n"
            "fastapi-infra = infra.cli:main\n"
            "\n"
            "[gui_scripts]\n"
            "fastapi-infra-gui = infra.gui:main\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "unexpected entry point section gui_scripts" in captured.err


def test_distribution_check_rejects_missing_wheel_license_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        include_license=False,
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "missing required wheel license file LICENSE" in captured.err


def test_distribution_check_accepts_declared_additional_license_file(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "License-File: NOTICE\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
        extra_files=("fastapi_infra-0.2.0.dist-info/licenses/NOTICE",),
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "License-File: NOTICE\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=_pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license-files = ["NOTICE"]',
            'requires-python = ">=3.11"',
        ),
        extra_files=("NOTICE",),
    )

    assert module.main([str(wheel), str(source)]) == 0


def test_distribution_check_rejects_missing_wheel_metadata_license_file(
    tmp_path,
    capsys,
) -> None:
    module = _load_script("scripts/check_distribution.py")
    metadata_content = (
        _wheel_metadata_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
            extras=(),
        )
        + "License-File: NOTICE\n"
    )
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        metadata_content=metadata_content,
    )
    pkg_info = (
        _pkg_info_content(
            name="fastapi-infra",
            version="0.2.0",
            requires_python=">=3.11",
            dependencies=(
                "fastapi>=0.117.1,<0.118.0",
                "uvicorn[standard]>=0.37.0,<0.38.0",
                "starlette>=0.48.0,<0.49.0",
                "pydantic>=2.11.0,<3.0.0",
                "pydantic-settings>=2.10.0,<3.0.0",
                "loguru>=0.7.0,<0.8.0",
            ),
        )
        + "License-File: NOTICE\n"
    )
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pkg_info=pkg_info,
        pyproject=_pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'license-files = ["NOTICE"]',
            'requires-python = ">=3.11"',
        ),
        extra_files=("NOTICE",),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel missing metadata license file NOTICE" in captured.err


def test_distribution_check_rejects_wheel_license_directory(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        directory_entries=("fastapi_infra-0.2.0.dist-info/licenses/LICENSE",),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "required wheel license file is not a regular file LICENSE" in captured.err


def test_distribution_check_rejects_missing_top_level_package(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        top_level="other\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel top_level.txt missing required top-level package infra" in captured.err


def test_distribution_check_rejects_unexpected_top_level_package(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        top_level="infra\nother\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel top_level.txt unexpected top-level package other" in captured.err


def test_distribution_check_rejects_duplicate_wheel_top_level_package(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        top_level="infra\ninfra\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel top_level.txt duplicate top-level package infra" in captured.err


def test_distribution_check_rejects_wheel_record_missing_required_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_content=(
            "infra/__init__.py,,\n"
            "infra/cli.py,,\n"
            "infra/scaffold.py,,\n"
            "fastapi_infra-0.2.0.dist-info/METADATA,,\n"
            "fastapi_infra-0.2.0.dist-info/WHEEL,,\n"
            "fastapi_infra-0.2.0.dist-info/entry_points.txt,,\n"
            "fastapi_infra-0.2.0.dist-info/top_level.txt,,\n"
            "fastapi_infra-0.2.0.dist-info/RECORD,,\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "wheel RECORD missing archive entry infra/provider_tests/test_live_providers.py"
    ) in captured.err


def test_distribution_check_rejects_wheel_record_entry_without_archive_file(
    tmp_path, capsys
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_content=(
            "infra/__init__.py,,\n"
            "infra/cli.py,,\n"
            "infra/scaffold.py,,\n"
            "infra/provider_tests/test_live_providers.py,,\n"
            "infra/missing.py,,\n"
            "fastapi_infra-0.2.0.dist-info/METADATA,,\n"
            "fastapi_infra-0.2.0.dist-info/WHEEL,,\n"
            "fastapi_infra-0.2.0.dist-info/entry_points.txt,,\n"
            "fastapi_infra-0.2.0.dist-info/top_level.txt,,\n"
            "fastapi_infra-0.2.0.dist-info/RECORD,,\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel RECORD references missing archive entry infra/missing.py" in captured.err


def test_distribution_check_rejects_invalid_wheel_record_row(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_content=(
            "infra/__init__.py\n"
            "infra/cli.py,,\n"
            "infra/scaffold.py,,\n"
            "infra/provider_tests/test_live_providers.py,,\n"
            "fastapi_infra-0.2.0.dist-info/METADATA,,\n"
            "fastapi_infra-0.2.0.dist-info/WHEEL,,\n"
            "fastapi_infra-0.2.0.dist-info/entry_points.txt,,\n"
            "fastapi_infra-0.2.0.dist-info/top_level.txt,,\n"
            "fastapi_infra-0.2.0.dist-info/RECORD,,\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid wheel RECORD row for infra/__init__.py: expected 3 columns" in captured.err


def test_distribution_check_rejects_missing_wheel_record_hash_and_size(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_content=(
            "infra/__init__.py,,\n"
            "infra/cli.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "infra/scaffold.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "infra/provider_tests/test_live_providers.py,"
            "sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "fastapi_infra-0.2.0.dist-info/METADATA,"
            "sha256=8XSdrWB-dz17IPWrLRFjrEF-WZR4sMNKVTj9YfZAvJY,34\n"
            "fastapi_infra-0.2.0.dist-info/WHEEL,"
            "sha256=AHkFS8lAXaeyj7jVnWdqOCr__GWc3uGb-fWzH9kB2rw,52\n"
            "fastapi_infra-0.2.0.dist-info/entry_points.txt,"
            "sha256=vr2RUdI6i2zi3nnOJq8a55crsYyKkhKLWV11hmfffOc,49\n"
            "fastapi_infra-0.2.0.dist-info/top_level.txt,"
            "sha256=QULNvXR7YSuRmB0OWJD88JBBz9PPFe9s9_4dS_WQpt0,6\n"
            "fastapi_infra-0.2.0.dist-info/RECORD,,\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel RECORD entry infra/__init__.py missing hash or size" in captured.err


def test_distribution_check_rejects_non_numeric_wheel_record_size(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_content=(
            "infra/__init__.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "infra/cli.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,not-a-number\n"
            "infra/scaffold.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "infra/provider_tests/test_live_providers.py,"
            "sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "fastapi_infra-0.2.0.dist-info/METADATA,"
            "sha256=8XSdrWB-dz17IPWrLRFjrEF-WZR4sMNKVTj9YfZAvJY,34\n"
            "fastapi_infra-0.2.0.dist-info/WHEEL,"
            "sha256=AHkFS8lAXaeyj7jVnWdqOCr__GWc3uGb-fWzH9kB2rw,52\n"
            "fastapi_infra-0.2.0.dist-info/entry_points.txt,"
            "sha256=vr2RUdI6i2zi3nnOJq8a55crsYyKkhKLWV11hmfffOc,49\n"
            "fastapi_infra-0.2.0.dist-info/top_level.txt,"
            "sha256=QULNvXR7YSuRmB0OWJD88JBBz9PPFe9s9_4dS_WQpt0,6\n"
            "fastapi_infra-0.2.0.dist-info/RECORD,,\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid wheel RECORD size for infra/cli.py: not-a-number" in captured.err


def test_distribution_check_rejects_non_sha256_wheel_record_hash(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_content=(
            "infra/__init__.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "infra/cli.py,md5=1B2M2Y8AsgTpgAmY7PhCfg,0\n"
            "infra/scaffold.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "infra/provider_tests/test_live_providers.py,"
            "sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "fastapi_infra-0.2.0.dist-info/METADATA,"
            "sha256=8XSdrWB-dz17IPWrLRFjrEF-WZR4sMNKVTj9YfZAvJY,34\n"
            "fastapi_infra-0.2.0.dist-info/WHEEL,"
            "sha256=AHkFS8lAXaeyj7jVnWdqOCr__GWc3uGb-fWzH9kB2rw,52\n"
            "fastapi_infra-0.2.0.dist-info/entry_points.txt,"
            "sha256=vr2RUdI6i2zi3nnOJq8a55crsYyKkhKLWV11hmfffOc,49\n"
            "fastapi_infra-0.2.0.dist-info/top_level.txt,"
            "sha256=QULNvXR7YSuRmB0OWJD88JBBz9PPFe9s9_4dS_WQpt0,6\n"
            "fastapi_infra-0.2.0.dist-info/RECORD,,\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid wheel RECORD hash algorithm for infra/cli.py: md5" in captured.err


def test_distribution_check_rejects_invalid_wheel_record_hash_value(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_content=(
            "infra/__init__.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "infra/cli.py,sha256=not-a-valid-digest!,0\n"
            "infra/scaffold.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "infra/provider_tests/test_live_providers.py,"
            "sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n"
            "fastapi_infra-0.2.0.dist-info/METADATA,"
            "sha256=8XSdrWB-dz17IPWrLRFjrEF-WZR4sMNKVTj9YfZAvJY,34\n"
            "fastapi_infra-0.2.0.dist-info/WHEEL,"
            "sha256=AHkFS8lAXaeyj7jVnWdqOCr__GWc3uGb-fWzH9kB2rw,52\n"
            "fastapi_infra-0.2.0.dist-info/entry_points.txt,"
            "sha256=vr2RUdI6i2zi3nnOJq8a55crsYyKkhKLWV11hmfffOc,49\n"
            "fastapi_infra-0.2.0.dist-info/top_level.txt,"
            "sha256=QULNvXR7YSuRmB0OWJD88JBBz9PPFe9s9_4dS_WQpt0,6\n"
            "fastapi_infra-0.2.0.dist-info/RECORD,,\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid wheel RECORD hash value for infra/cli.py: not-a-valid-digest!" in captured.err


def test_distribution_check_rejects_duplicate_wheel_record_entry(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_extra="infra/cli.py,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "duplicate wheel RECORD entry infra/cli.py" in captured.err


def test_distribution_check_rejects_record_hash_for_record_file(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_self_fields="sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,1",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "wheel RECORD entry fastapi_infra-0.2.0.dist-info/RECORD must not include hash or size"
        in (captured.err)
    )


def test_distribution_check_rejects_blank_hash_for_non_self_record_named_file(
    tmp_path, capsys
) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_extra="infra/nested.dist-info/RECORD,,\n",
    )
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("infra/nested.dist-info/RECORD", "")
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel RECORD entry infra/nested.dist-info/RECORD missing hash or size" in captured.err


def test_distribution_check_rejects_empty_wheel_record_path(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_extra=",,\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "invalid wheel RECORD row for <empty>: path is required" in captured.err


def test_distribution_check_rejects_unsafe_wheel_record_path(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_extra="../evil.py,,\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "unsafe wheel RECORD entry ../evil.py" in captured.err


def test_distribution_check_rejects_directory_wheel_record_path(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_extra="infra/,,\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel RECORD entry infra/ must not reference a directory" in captured.err


def test_distribution_check_rejects_empty_path_segment_wheel_record_path(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        record_extra="infra//evil.py,,\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "unsafe wheel RECORD entry infra//evil.py" in captured.err


def test_distribution_check_rejects_wheel_tag_mismatch(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        wheel_metadata=(
            "Wheel-Version: 1.0\n" "Root-Is-Purelib: true\n" "Tag: cp311-cp311-macosx_11_0_arm64\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "wheel tag mismatch: filename py3-none-any, " "WHEEL cp311-cp311-macosx_11_0_arm64"
    ) in captured.err


def test_distribution_check_rejects_missing_wheel_version_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        wheel_metadata="Root-Is-Purelib: true\nTag: py3-none-any\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel WHEEL missing Wheel-Version: 1.0" in captured.err


def test_distribution_check_rejects_duplicate_wheel_version_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        wheel_metadata=(
            "Wheel-Version: 1.0\n"
            "Wheel-Version: 1.0\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel WHEEL duplicate Wheel-Version metadata field" in captured.err


def test_distribution_check_rejects_duplicate_wheel_tag_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        wheel_metadata=(
            "Wheel-Version: 1.0\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
            "Tag: py3-none-any\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel WHEEL duplicate Tag metadata py3-none-any" in captured.err


def test_distribution_check_rejects_unexpected_wheel_tag_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        wheel_metadata=(
            "Wheel-Version: 1.0\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
            "Tag: cp311-cp311-macosx_11_0_arm64\n"
        ),
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel WHEEL unexpected Tag metadata cp311-cp311-macosx_11_0_arm64" in (captured.err)


def test_distribution_check_rejects_non_purelib_wheel_metadata(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(
        tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl",
        wheel_metadata="Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n",
    )
    source = _write_clean_sdist(tmp_path / "fastapi_infra-0.2.0.tar.gz")

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "wheel WHEEL Root-Is-Purelib must be true" in captured.err


def test_distribution_check_rejects_missing_sdist_console_script(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject='[project]\nname = "fastapi-infra"\nversion = "0.2.0"\n',
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert "sdist pyproject.toml missing project.scripts.fastapi-infra = infra.cli:main" in (
        captured.err
    )


def test_distribution_check_rejects_non_table_pyproject_scripts(tmp_path, capsys) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'requires-python = ">=3.11"',
            'scripts = "infra.cli:main"',
            scripts=None,
        ),
        "pyproject invalid project.scripts table 'infra.cli:main'",
    )


def test_distribution_check_rejects_non_string_pyproject_console_script(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'requires-python = ">=3.11"',
            scripts="fastapi-infra = 123",
        ),
        "pyproject invalid project.scripts.fastapi-infra 123",
    )


def test_distribution_check_rejects_pyproject_console_script_with_outer_whitespace(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'requires-python = ">=3.11"',
            scripts='fastapi-infra = " infra.cli:main"',
        ),
        "pyproject invalid project.scripts.fastapi-infra ' infra.cli:main'",
    )


def test_distribution_check_rejects_unexpected_pyproject_console_script(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'requires-python = ">=3.11"',
            scripts=('fastapi-infra = "infra.cli:main"\n' 'fastapi-infra-dev = "infra.dev:main"'),
        ),
        "pyproject unexpected project.scripts.fastapi-infra-dev = infra.dev:main",
    )


def test_distribution_check_rejects_non_string_unexpected_pyproject_console_script(
    tmp_path,
    capsys,
) -> None:
    _assert_distribution_check_rejects_pyproject(
        tmp_path,
        capsys,
        _pyproject_with_core_metadata(
            'name = "fastapi-infra"',
            'version = "0.2.0"',
            'requires-python = ">=3.11"',
            scripts='fastapi-infra = "infra.cli:main"\nfastapi-infra-dev = 123',
        ),
        "pyproject invalid project.scripts.fastapi-infra-dev 123",
    )


def test_distribution_check_rejects_sdist_pyproject_metadata_mismatch(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        pyproject=(
            "[project]\n"
            'name = "fastapi-infra-other"\n'
            'version = "0.2.0"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        ),
    )

    assert module.main([str(wheel), str(source)]) == 1
    captured = capsys.readouterr()
    assert (
        "sdist pyproject.toml metadata mismatch: filename fastapi-infra 0.2.0, "
        "pyproject fastapi-infra-other 0.2.0"
    ) in captured.err


def test_distribution_check_reports_unsupported_artifacts(tmp_path, capsys) -> None:
    module = _load_script("scripts/check_distribution.py")
    unsupported = tmp_path / "notes.txt"
    unsupported.write_text("not a distribution artifact", encoding="utf-8")

    assert module.main([str(unsupported)]) == 1
    captured = capsys.readouterr()
    assert "unsupported distribution artifact" in captured.err


def test_smoke_scripts_do_not_shadow_installed_infra_for_wheel_smoke() -> None:
    generated_smoke = Path("scripts/smoke_generated_projects.py").read_text(encoding="utf-8")
    template_smoke = Path("scripts/smoke_plugin_templates.py").read_text(encoding="utf-8")

    assert "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))" not in generated_smoke
    assert template_smoke.count("sys.path.insert(0, str(Path(__file__).resolve().parents[1]))") == 1
    assert template_smoke.index("except ModuleNotFoundError:") < template_smoke.index(
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))"
    )


def _write_clean_wheel(
    path: Path,
    *,
    metadata_name: str = "fastapi-infra",
    metadata_version: str | None = None,
    dist_info_dir: str = "fastapi_infra-0.2.0.dist-info",
    wheel_metadata: str = "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    entry_points: str = "[console_scripts]\nfastapi-infra = infra.cli:main\n",
    top_level: str = "infra\n",
    metadata_requires_python: str = ">=3.11",
    metadata_dependencies: tuple[str, ...] = DEFAULT_METADATA_DEPENDENCIES,
    metadata_extras: tuple[str, ...] = (),
    metadata_content: str | bytes | None = None,
    record_content: str | None = None,
    record_extra: str = "",
    record_self_fields: str = ",",
    symlink_entries: Mapping[str, str] | None = None,
    directory_entries: tuple[str, ...] = (),
    extra_files: tuple[str, ...] = (),
    include_license: bool = True,
) -> Path:
    entries = [
        "infra/__init__.py",
        "infra/cli.py",
        "infra/scaffold.py",
        "infra/provider_tests/test_live_providers.py",
        *extra_files,
    ]
    version = metadata_version or path.name.split("-", 2)[1]
    directories = set(directory_entries)
    archive_contents: dict[str, str | bytes] = {
        entry: "" for entry in entries if entry not in directories
    }
    links = symlink_entries or {}
    archive_contents.update(links)
    archive_contents.update(
        {
            f"{dist_info_dir}/METADATA": (
                metadata_content
                if metadata_content is not None
                else _wheel_metadata_content(
                    name=metadata_name,
                    version=version,
                    requires_python=metadata_requires_python,
                    dependencies=metadata_dependencies,
                    extras=metadata_extras,
                )
            ),
            f"{dist_info_dir}/WHEEL": wheel_metadata,
            f"{dist_info_dir}/entry_points.txt": entry_points,
            f"{dist_info_dir}/top_level.txt": top_level,
        }
    )
    if include_license:
        archive_contents[f"{dist_info_dir}/licenses/LICENSE"] = ""
    for entry in directories:
        archive_contents.pop(entry, None)
    record_path = f"{dist_info_dir}/RECORD"
    record_body = (
        record_content
        if record_content is not None
        else _wheel_record_content(archive_contents, record_path, record_self_fields)
    )
    with zipfile.ZipFile(path, "w") as archive:
        for entry in sorted(directories):
            info = zipfile.ZipInfo(f"{entry}/")
            info.create_system = 3
            info.external_attr = (stat.S_IFDIR | 0o755) << 16
            archive.writestr(info, "")
        for entry, content in archive_contents.items():
            if entry in links:
                info = zipfile.ZipInfo(entry)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, content)
            else:
                archive.writestr(entry, content)
        archive.writestr(record_path, record_body + record_extra)
    return path


def _wheel_metadata_content(
    *,
    name: str,
    version: str,
    requires_python: str,
    dependencies: tuple[str, ...],
    extras: tuple[str, ...],
) -> str:
    return _core_metadata_content(
        name=name,
        version=version,
        requires_python=requires_python,
        dependencies=dependencies,
        extras=extras,
    )


def _pkg_info_content(
    *,
    name: str,
    version: str,
    requires_python: str,
    dependencies: tuple[str, ...],
    extras: tuple[str, ...] = (),
) -> str:
    return _core_metadata_content(
        name=name,
        version=version,
        requires_python=requires_python,
        dependencies=dependencies,
        extras=extras,
    )


def _core_metadata_content(
    *,
    name: str,
    version: str,
    requires_python: str,
    dependencies: tuple[str, ...],
    extras: tuple[str, ...] = (),
) -> str:
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"Requires-Python: {requires_python}",
    ]
    lines.extend(f"Requires-Dist: {dependency}" for dependency in dependencies)
    lines.extend(f"Provides-Extra: {extra}" for extra in extras)
    return "\n".join(lines) + "\n"


def _requires_txt_content() -> str:
    return DEFAULT_REQUIRES_TXT


def _sources_txt_content(entries: Sequence[str]) -> str:
    return "\n".join(entry for entry in entries if entry != "setup.cfg") + "\n"


def _wheel_record_content(
    archive_contents: dict[str, str | bytes], record_path: str, record_self_fields: str
) -> str:
    records = []
    for entry, content in archive_contents.items():
        content_bytes = _content_bytes(content)
        records.append(f"{entry},{_record_hash(content_bytes)},{len(content_bytes)}\n")
    records.append(f"{record_path},{record_self_fields}\n")
    return "".join(records)


def _content_bytes(content: str | bytes) -> bytes:
    if isinstance(content, bytes):
        return content
    return content.encode("utf-8")


def _record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii")
    return f"sha256={digest.rstrip('=')}"


def _write_clean_sdist(
    path: Path,
    *,
    metadata_name: str = "fastapi-infra",
    metadata_version: str | None = None,
    pkg_info: str | bytes | None = None,
    pyproject: str | None = None,
    root_dir: str | None = None,
    root_files: tuple[str, ...] = (),
    include_root_dir_entry: bool = False,
    symlink_entries: Mapping[str, str] | None = None,
    root_symlink_target: str | None = None,
    directory_entries: tuple[str, ...] = (),
    extra_files: tuple[str, ...] = (),
    extra_file_contents: Mapping[str, str | bytes] | None = None,
) -> Path:
    root = root_dir or path.name.removesuffix(".tar.gz").removesuffix(".tgz")
    version = metadata_version or root.rsplit("-", 1)[1]
    entries = _clean_sdist_entries(extra_files)
    pyproject_content = pyproject if pyproject is not None else _default_pyproject_content(version)
    links = symlink_entries or {}
    directories = set(directory_entries)
    extra_contents = extra_file_contents or {}
    with tarfile.open(path, "w:gz") as archive:
        _add_sdist_root_entry(
            archive,
            path.parent / root,
            root,
            root_symlink_target=root_symlink_target,
            include_root_dir_entry=include_root_dir_entry,
        )
        for entry in entries:
            _add_clean_sdist_entry(
                archive,
                path.parent / root,
                root,
                entry,
                entries=entries,
                directories=directories,
                links=links,
                extra_contents=extra_contents,
                metadata_name=metadata_name,
                version=version,
                pkg_info=pkg_info,
                pyproject_content=pyproject_content,
            )
        for entry in root_files:
            source = path.parent / entry
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("", encoding="utf-8")
            archive.add(source, arcname=entry)
    return path


def _clean_sdist_entries(extra_files: tuple[str, ...]) -> list[str]:
    return [
        "infra/__init__.py",
        "infra/cli.py",
        "infra/scaffold.py",
        "infra/provider_tests/test_live_providers.py",
        "PKG-INFO",
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "pyproject.toml",
        *extra_files,
    ]


def _default_pyproject_content(version: str) -> str:
    return (
        "[project]\n"
        'name = "fastapi-infra"\n'
        f'version = "{version}"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = [\n"
        + "".join(f'    "{dependency}",\n' for dependency in DEFAULT_METADATA_DEPENDENCIES)
        + "]\n"
        "\n"
        "[project.scripts]\n"
        'fastapi-infra = "infra.cli:main"\n'
    )


def _add_sdist_root_entry(
    archive: tarfile.TarFile,
    root_source: Path,
    root: str,
    *,
    root_symlink_target: str | None,
    include_root_dir_entry: bool,
) -> None:
    if root_symlink_target is not None:
        info = tarfile.TarInfo(root)
        info.type = tarfile.SYMTYPE
        info.linkname = root_symlink_target
        archive.addfile(info)
    if include_root_dir_entry:
        root_source.mkdir(parents=True, exist_ok=True)
        archive.add(root_source, arcname=root, recursive=False)


def _add_clean_sdist_entry(
    archive: tarfile.TarFile,
    root_source: Path,
    root: str,
    entry: str,
    *,
    entries: Sequence[str],
    directories: set[str],
    links: Mapping[str, str],
    extra_contents: Mapping[str, str | bytes],
    metadata_name: str,
    version: str,
    pkg_info: str | bytes | None,
    pyproject_content: str,
) -> None:
    source = root_source / entry
    source.parent.mkdir(parents=True, exist_ok=True)
    if entry in directories:
        source.mkdir(parents=True, exist_ok=True)
        archive.add(source, arcname=f"{root}/{entry}", recursive=False)
        return
    if entry in links:
        info = tarfile.TarInfo(f"{root}/{entry}")
        info.type = tarfile.SYMTYPE
        info.linkname = links[entry]
        archive.addfile(info)
        return
    content = _clean_sdist_entry_content(
        entry,
        entries=entries,
        extra_contents=extra_contents,
        metadata_name=metadata_name,
        version=version,
        pkg_info=pkg_info,
        pyproject_content=pyproject_content,
    )
    _write_sdist_source(source, content)
    archive.add(source, arcname=f"{root}/{entry}")


def _clean_sdist_entry_content(
    entry: str,
    *,
    entries: Sequence[str],
    extra_contents: Mapping[str, str | bytes],
    metadata_name: str,
    version: str,
    pkg_info: str | bytes | None,
    pyproject_content: str,
) -> str | bytes:
    if entry == "PKG-INFO":
        return pkg_info or _pkg_info_content(
            name=metadata_name,
            version=version,
            requires_python=">=3.11",
            dependencies=DEFAULT_METADATA_DEPENDENCIES,
        )
    if entry == "pyproject.toml":
        return pyproject_content
    if entry in extra_contents:
        return extra_contents[entry]
    if entry == "fastapi_infra.egg-info/PKG-INFO":
        return _pkg_info_content(
            name=metadata_name,
            version=version,
            requires_python=">=3.11",
            dependencies=DEFAULT_METADATA_DEPENDENCIES,
        )
    if entry == "fastapi_infra.egg-info/entry_points.txt":
        return "[console_scripts]\nfastapi-infra = infra.cli:main\n"
    if entry == "fastapi_infra.egg-info/requires.txt":
        return _requires_txt_content()
    if entry == "fastapi_infra.egg-info/SOURCES.txt":
        return _sources_txt_content(entries)
    if entry == "fastapi_infra.egg-info/top_level.txt":
        return "infra\n"
    return ""


def _write_sdist_source(source: Path, content: str | bytes) -> None:
    if isinstance(content, bytes):
        source.write_bytes(content)
    else:
        source.write_text(content, encoding="utf-8")


def test_generated_project_smoke_script_writes_safe_ci_env(tmp_path) -> None:
    module = _load_script("scripts/smoke_generated_projects.py")
    source = tmp_path / ".env.example"
    target = tmp_path / ".env"
    source.write_text(
        "APP_NAME=demo\n"
        "JWT_SECRET=replace-with-32-byte-random-secret\n"
        "SEARCH_API_KEY=\n"
        "REDIS_URL=redis://localhost:6379/0\n",
        encoding="utf-8",
    )

    module._write_ci_env(source, target)

    assert target.read_text(encoding="utf-8") == (
        "APP_NAME=demo\n"
        "JWT_SECRET=ci-jwt-secret-at-least-32-characters-long\n"
        "SEARCH_API_KEY=ci-search-api-key\n"
        "REDIS_URL=redis://localhost:6379/0\n"
    )


def test_generated_project_smoke_script_can_fill_provider_placeholders(tmp_path) -> None:
    module = _load_script("scripts/smoke_generated_projects.py")
    source = tmp_path / "provider.env"
    source.write_text(
        "\n".join(
            [
                "MYSQL_LIVE_HOST=",
                "# MYSQL_LIVE_PORT=",
                "REDIS_LIVE_URL=redis://localhost:6379/0",
                "",
            ]
        ),
        encoding="utf-8",
    )

    module._write_ci_env(source, source, blank_value_prefix="ci-provider")

    assert source.read_text(encoding="utf-8") == (
        "MYSQL_LIVE_HOST=ci-provider-mysql_live_host\n"
        "# MYSQL_LIVE_PORT=\n"
        "REDIS_LIVE_URL=redis://localhost:6379/0\n"
    )


def test_generated_project_smoke_script_uses_generated_makefile(tmp_path, monkeypatch) -> None:
    module = _load_script("scripts/smoke_generated_projects.py")
    project_dir = tmp_path / "api_app"
    project_dir.mkdir()
    commands = []
    env_overlays = []

    def fake_run(command, *, cwd, timeout):
        commands.append((command, cwd, timeout))

    def fake_write_ci_env(source, target, **kwargs):
        env_overlays.append((source, target, kwargs))

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module, "_write_ci_env", fake_write_ci_env)

    module._smoke_profile(
        "api",
        work_dir=tmp_path,
        python=Path("python"),
        timeout=7,
        keep_existing=True,
    )

    assert commands == [
        (["make", "env"], project_dir, 7),
        (["make", "verify"], project_dir, 7),
        (["make", "release-static"], project_dir, 7),
        (["scripts/verify-release.sh", ".env", "provider.env"], project_dir, 7),
    ]
    assert env_overlays == [
        (project_dir / ".env", project_dir / ".env", {}),
        (
            project_dir / "provider.env",
            project_dir / "provider.env",
            {"blank_value_prefix": "ci-provider"},
        ),
    ]


def test_generated_project_smoke_script_installs_external_plugin_to_private_target(
    tmp_path, monkeypatch
) -> None:
    module = _load_script("scripts/smoke_generated_projects.py")
    package_dir = tmp_path / "plugin"
    target_dir = tmp_path / "target"
    package_dir.mkdir()
    (package_dir / "pyproject.toml").write_text("[project]\nname = 'plugin'\n")
    commands = []

    def fake_run(command, *, cwd, timeout, env=None):
        commands.append((command, cwd, timeout, env))

    monkeypatch.setattr(module, "_run", fake_run)

    env = module._install_editable_plugin(
        package_dir,
        target_dir=target_dir,
        python=Path("python"),
        timeout=7,
    )

    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(target_dir.resolve())
    assert commands == [
        (
            [
                "python",
                "-m",
                "pip",
                "install",
                "-e",
                str(package_dir.resolve()),
                "--no-deps",
                "--no-build-isolation",
                "--target",
                str(target_dir),
            ],
            package_dir.resolve(),
            7,
            None,
        )
    ]


def test_generated_project_smoke_script_private_target_includes_editable_pth_paths(
    tmp_path, monkeypatch
) -> None:
    module = _load_script("scripts/smoke_generated_projects.py")
    target_dir = tmp_path / "target"
    src_dir = tmp_path / "plugin" / "src"
    target_dir.mkdir()
    src_dir.mkdir(parents=True)
    (target_dir / "editable.pth").write_text(
        f"# ignored\n{src_dir}\nimport editable_hook\nrelative-src\n",
        encoding="utf-8",
    )
    (target_dir / "relative-src").mkdir()
    monkeypatch.setenv("PYTHONPATH", "existing-path")

    entries = module._pythonpath_entries_for_target(target_dir)
    env = module._pythonpath_env(entries)

    assert entries == [
        target_dir.resolve(),
        src_dir.resolve(),
        (target_dir / "relative-src").resolve(),
    ]
    assert env["PYTHONPATH"] == os.pathsep.join(
        [
            str(target_dir.resolve()),
            str(src_dir.resolve()),
            str((target_dir / "relative-src").resolve()),
            "existing-path",
        ]
    )


def test_generated_project_smoke_script_has_external_plugin_mode() -> None:
    module = _load_script("scripts/smoke_generated_projects.py")
    parser = module._build_parser()

    args = parser.parse_args(
        [
            "--work-dir",
            "/tmp/generated",
            "--external-plugin-example",
            "examples/search_plugin",
        ]
    )

    assert args.external_plugin_example == Path("examples/search_plugin")


def test_plugin_manifest_env_placeholders_are_declared_env_vars() -> None:
    manifest = PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins()).manifest()

    for plugin_name, plugin in manifest.items():
        env_vars = plugin["env_vars"]
        assert isinstance(env_vars, list)
        declared_env_vars = {env_var for env_var in env_vars if isinstance(env_var, str)}
        config_placeholders = _env_placeholders(plugin["local_config_example"]) | _env_placeholders(
            plugin["production_config_example"]
        )

        assert config_placeholders <= declared_env_vars, plugin_name


def test_plugin_manifest_config_examples_validate_against_plugin_config_models() -> None:
    plugins = get_builtin_plugins()
    manifest = PluginManager(settings=InfraSettings(), plugins=plugins).manifest()

    for plugin in plugins:
        plugin_manifest = manifest[plugin.metadata.name]
        for key in ("local_config_example", "production_config_example"):
            plugin_configs = {
                plugin.metadata.name: {
                    "enabled": True,
                    "config": _replace_env_placeholders(plugin_manifest[key]),
                }
            }
            if key == "production_config_example":
                for dependency in cast(list[str], plugin_manifest["production_dependencies"]):
                    dependency_manifest = manifest[dependency]
                    plugin_configs[dependency] = {
                        "enabled": True,
                        "config": _replace_env_placeholders(
                            dependency_manifest["production_config_example"]
                        ),
                    }
            settings = InfraSettings.model_validate(
                {
                    "infra": {
                        "plugins": plugin_configs,
                    }
                }
            )

            assert validate_infra_settings(settings, plugins) == [], (
                plugin.metadata.name,
                key,
            )


def test_plugin_manifest_production_dependencies_are_known_plugins() -> None:
    manifest = PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins()).manifest()

    for plugin_name, plugin in manifest.items():
        production_dependencies = plugin["production_dependencies"]
        assert isinstance(production_dependencies, list)
        assert set(production_dependencies) <= set(manifest), plugin_name


def test_plugin_manifest_service_keys_cover_default_services() -> None:
    manifest = PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins()).manifest()

    for plugin_name, plugin in manifest.items():
        service_keys = plugin["service_keys"]
        assert isinstance(service_keys, dict)
        for service_name in cast(list[str], plugin["provides"]):
            if isinstance(service_name, str):
                assert service_name in service_keys, plugin_name
                assert service_keys[service_name].startswith("infra.plugins."), plugin_name
                service_key = _load_object(service_keys[service_name])
                assert isinstance(service_key, ServiceKey), plugin_name
                assert service_key.name == service_name, plugin_name


def test_plugin_manifest_service_references_are_config_fields() -> None:
    manifest = PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins()).manifest()

    for plugin_name, plugin in manifest.items():
        service_references = plugin["service_references"]
        assert isinstance(service_references, dict)
        config_schema = plugin["config_schema"]
        config_properties = (
            set(config_schema.get("properties", {}))
            if isinstance(config_schema, Mapping)
            else set()
        )
        for field_name, reference in service_references.items():
            assert field_name.split(".", maxsplit=1)[0] in config_properties, plugin_name
            assert isinstance(reference, Mapping), plugin_name
            assert set(reference) == {
                "default_service",
                "required_when",
                "required_when_config",
                "required_unless_config",
                "optional",
                "description",
            }
            assert isinstance(reference["optional"], bool), plugin_name
            assert isinstance(reference["required_when_config"], Mapping), plugin_name
            assert isinstance(reference["required_unless_config"], Mapping), plugin_name


def test_plugin_manifest_migrations_are_valid_sql_migration_specs() -> None:
    manifest = PluginManager(settings=InfraSettings(), plugins=get_builtin_plugins()).manifest()
    seen_versions: set[str] = set()

    for plugin_name, plugin in manifest.items():
        migrations = plugin["migrations"]
        assert isinstance(migrations, list)
        for migration in migrations:
            assert isinstance(migration, Mapping), plugin_name
            assert re.fullmatch(r"[0-9]{14}", migration.get("version", "")), plugin_name
            assert re.fullmatch(r"[a-z][a-z0-9_]*", migration.get("name", "")), plugin_name
            assert isinstance(migration.get("sql"), str) and "CREATE TABLE" in migration["sql"]
            version = migration["version"]
            assert version not in seen_versions
            seen_versions.add(version)


def _env_placeholders(value: Any) -> set[str]:
    if isinstance(value, str):
        match = ENV_PLACEHOLDER_RE.fullmatch(value)
        return {match.group(1)} if match is not None else set()
    if isinstance(value, Mapping):
        placeholders: set[str] = set()
        for item in value.values():
            placeholders.update(_env_placeholders(item))
        return placeholders
    if isinstance(value, list):
        list_placeholders: set[str] = set()
        for item in value:
            list_placeholders.update(_env_placeholders(item))
        return list_placeholders
    return set()


def _replace_env_placeholders(value: Any) -> Any:
    if isinstance(value, str):
        match = ENV_PLACEHOLDER_RE.fullmatch(value)
        if match is not None:
            return ENV_EXAMPLE_VALUES[match.group(1)]
        return value
    if isinstance(value, Mapping):
        return {key: _replace_env_placeholders(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_env_placeholders(item) for item in value]
    return value


def _load_object(path: str) -> object:
    module_name, _, object_name = path.rpartition(".")
    assert module_name and object_name
    module = importlib.import_module(module_name)
    return getattr(module, object_name)


def _load_script(path: str):
    spec = importlib.util.spec_from_file_location("smoke_generated_projects", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
