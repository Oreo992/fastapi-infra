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
from typing import Any, cast

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


def test_httpx_dependency_range_is_consistent_for_testclient_users() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    optional_dependencies = pyproject["project"]["optional-dependencies"]

    assert "httpx>=0.27.0,<0.29.0" in optional_dependencies["http"]
    assert "httpx>=0.27.0,<0.29.0" in optional_dependencies["dev"]


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


def test_distribution_check_accepts_sdist_root_directory_entry(tmp_path) -> None:
    module = _load_script("scripts/check_distribution.py")
    wheel = _write_clean_wheel(tmp_path / "fastapi_infra-0.2.0-py3-none-any.whl")
    source = _write_clean_sdist(
        tmp_path / "fastapi_infra-0.2.0.tar.gz",
        include_root_dir_entry=True,
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
    record_content: str | None = None,
    record_extra: str = "",
    record_self_fields: str = ",",
    symlink_entries: Mapping[str, str] | None = None,
    directory_entries: tuple[str, ...] = (),
    include_license: bool = True,
) -> Path:
    entries = [
        "infra/__init__.py",
        "infra/cli.py",
        "infra/scaffold.py",
        "infra/provider_tests/test_live_providers.py",
    ]
    version = metadata_version or path.name.split("-", 2)[1]
    directories = set(directory_entries)
    archive_contents = {entry: "" for entry in entries if entry not in directories}
    links = symlink_entries or {}
    archive_contents.update(links)
    archive_contents.update(
        {
            f"{dist_info_dir}/METADATA": f"Name: {metadata_name}\nVersion: {version}\n",
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


def _wheel_record_content(
    archive_contents: dict[str, str], record_path: str, record_self_fields: str
) -> str:
    records = [
        f"{entry},{_record_hash(content.encode('utf-8'))},{len(content.encode('utf-8'))}\n"
        for entry, content in archive_contents.items()
    ]
    records.append(f"{record_path},{record_self_fields}\n")
    return "".join(records)


def _record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii")
    return f"sha256={digest.rstrip('=')}"


def _write_clean_sdist(
    path: Path,
    *,
    metadata_name: str = "fastapi-infra",
    metadata_version: str | None = None,
    pyproject: str | None = None,
    root_dir: str | None = None,
    root_files: tuple[str, ...] = (),
    include_root_dir_entry: bool = False,
    symlink_entries: Mapping[str, str] | None = None,
    root_symlink_target: str | None = None,
    directory_entries: tuple[str, ...] = (),
) -> Path:
    entries = [
        "infra/__init__.py",
        "infra/cli.py",
        "infra/scaffold.py",
        "infra/provider_tests/test_live_providers.py",
        "PKG-INFO",
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "pyproject.toml",
    ]
    root = root_dir or path.name.removesuffix(".tar.gz").removesuffix(".tgz")
    version = metadata_version or root.rsplit("-", 1)[1]
    pyproject_content = (
        pyproject
        if pyproject is not None
        else (
            "[project]\n"
            'name = "fastapi-infra"\n'
            f'version = "{version}"\n'
            "\n"
            "[project.scripts]\n"
            'fastapi-infra = "infra.cli:main"\n'
        )
    )
    links = symlink_entries or {}
    directories = set(directory_entries)
    with tarfile.open(path, "w:gz") as archive:
        if root_symlink_target is not None:
            info = tarfile.TarInfo(root)
            info.type = tarfile.SYMTYPE
            info.linkname = root_symlink_target
            archive.addfile(info)
        if include_root_dir_entry:
            root_source = path.parent / root
            root_source.mkdir(parents=True, exist_ok=True)
            archive.add(root_source, arcname=root, recursive=False)
        for entry in entries:
            source = path.parent / root / entry
            source.parent.mkdir(parents=True, exist_ok=True)
            if entry in directories:
                source.mkdir(parents=True, exist_ok=True)
                archive.add(source, arcname=f"{root}/{entry}", recursive=False)
                continue
            if entry in links:
                info = tarfile.TarInfo(f"{root}/{entry}")
                info.type = tarfile.SYMTYPE
                info.linkname = links[entry]
                archive.addfile(info)
                continue
            if entry == "PKG-INFO":
                content = f"Name: {metadata_name}\nVersion: {version}\n"
            elif entry == "pyproject.toml":
                content = pyproject_content
            else:
                content = ""
            source.write_text(content, encoding="utf-8")
            archive.add(source, arcname=f"{root}/{entry}")
        for entry in root_files:
            source = path.parent / entry
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("", encoding="utf-8")
            archive.add(source, arcname=entry)
    return path


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
