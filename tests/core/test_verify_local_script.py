from __future__ import annotations

import importlib.util
from pathlib import Path


def test_verify_local_runs_core_checks_by_default(monkeypatch) -> None:
    module = _load_script("scripts/verify_local.py")
    calls: list[tuple[str, ...]] = []

    def fake_run(command: list[str], check: bool) -> object:
        calls.append(tuple(command))
        assert check is False

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main([]) == 0

    assert calls == [
        (module.sys.executable, "-m", "black", "--check", "infra", "tests", "scripts"),
        (module.sys.executable, "-m", "isort", "--check-only", "infra", "tests", "scripts"),
        (module.sys.executable, "scripts/typecheck.py"),
        (module.sys.executable, "-m", "pytest", "tests", "-q"),
    ]


def test_verify_local_can_include_package_and_smoke_checks(monkeypatch) -> None:
    module = _load_script("scripts/verify_local.py")
    calls: list[tuple[str, ...]] = []

    def fake_run(command: list[str], check: bool) -> object:
        calls.append(tuple(command))

        class Result:
            returncode = 0

        return Result()

    artifact_roots: list[Path] = []

    def fake_dist_artifacts(dist_dir: Path) -> list[str]:
        artifact_roots.append(dist_dir)
        return [
            str(dist_dir / "fastapi_infra-0.2.0-py3-none-any.whl"),
            str(dist_dir / "fastapi_infra-0.2.0.tar.gz"),
        ]

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_dist_artifacts", fake_dist_artifacts)

    assert module.main(["--package", "--smoke"]) == 0

    package_dist_dir = artifact_roots[0]
    wheel_path = str(package_dist_dir / "fastapi_infra-0.2.0-py3-none-any.whl")
    sdist_path = str(package_dist_dir / "fastapi_infra-0.2.0.tar.gz")
    build_index = calls.index(
        (module.sys.executable, "-m", "build", "--outdir", str(package_dist_dir))
    )

    assert calls[build_index + 1] == (
        module.sys.executable,
        "-m",
        "twine",
        "check",
        wheel_path,
        sdist_path,
    )
    assert calls[build_index + 2] == (
        module.sys.executable,
        "scripts/check_distribution.py",
        wheel_path,
        sdist_path,
    )
    assert calls[build_index + 3] == (
        module.sys.executable,
        "-m",
        "venv",
        calls[build_index + 3][3],
    )
    base_wheel_python = calls[build_index + 4][0]
    assert calls[build_index + 4] == (
        base_wheel_python,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
    )
    assert calls[build_index + 5] == (
        base_wheel_python,
        "-m",
        "pip",
        "install",
        wheel_path,
    )
    assert calls[build_index + 6][:3] == (base_wheel_python, "-c", module.BASE_WHEEL_SMOKE)

    assert calls[build_index + 7] == (
        module.sys.executable,
        "-m",
        "venv",
        calls[build_index + 7][3],
    )
    wheel_smoke_python = calls[build_index + 8][0]
    assert calls[build_index + 8] == (
        wheel_smoke_python,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
        "setuptools",
    )
    assert calls[build_index + 9] == (
        wheel_smoke_python,
        "-m",
        "pip",
        "install",
        f"{wheel_path}[dev]",
    )
    assert calls[build_index + 10] == (
        wheel_smoke_python,
        "scripts/smoke_generated_projects.py",
        "--work-dir",
        "/tmp/fastapi-infra-generated-smoke",
        "--external-plugin-example",
        "examples/search_plugin",
    )
    assert calls[build_index + 11] == (
        wheel_smoke_python,
        "scripts/smoke_plugin_templates.py",
        "--work-dir",
        "/tmp/fastapi-infra-plugin-template-smoke",
    )
    assert calls[build_index + 12] == (
        module.sys.executable,
        "-m",
        "venv",
        calls[build_index + 12][3],
    )
    observability_python = calls[build_index + 13][0]
    assert calls[build_index + 13] == (
        observability_python,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
    )
    assert calls[build_index + 14] == (
        observability_python,
        "-m",
        "pip",
        "install",
        f"{wheel_path}[observability]",
    )
    assert calls[build_index + 15] == (
        observability_python,
        "-c",
        module.OBSERVABILITY_WHEEL_SMOKE,
    )


def test_verify_local_can_run_package_checks_without_core_checks(monkeypatch) -> None:
    module = _load_script("scripts/verify_local.py")
    calls: list[tuple[str, ...]] = []

    def fake_run(command: list[str], check: bool) -> object:
        calls.append(tuple(command))

        class Result:
            returncode = 0

        return Result()

    artifact_roots: list[Path] = []

    def fake_dist_artifacts(dist_dir: Path) -> list[str]:
        artifact_roots.append(dist_dir)
        return [str(dist_dir / "package.whl"), str(dist_dir / "package.tar.gz")]

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_dist_artifacts", fake_dist_artifacts)

    assert module.main(["--skip-core", "--package"]) == 0

    package_dist_dir = artifact_roots[0]
    assert calls == [
        (module.sys.executable, "-m", "build", "--outdir", str(package_dist_dir)),
        (
            module.sys.executable,
            "-m",
            "twine",
            "check",
            str(package_dist_dir / "package.whl"),
            str(package_dist_dir / "package.tar.gz"),
        ),
        (
            module.sys.executable,
            "scripts/check_distribution.py",
            str(package_dist_dir / "package.whl"),
            str(package_dist_dir / "package.tar.gz"),
        ),
    ]


def test_verify_local_can_write_package_artifacts_to_explicit_empty_dist_dir(
    tmp_path, monkeypatch
) -> None:
    module = _load_script("scripts/verify_local.py")
    calls: list[tuple[str, ...]] = []
    dist_dir = tmp_path / "release-dist"

    def fake_run(command: list[str], check: bool) -> object:
        calls.append(tuple(command))

        class Result:
            returncode = 0

        return Result()

    def fake_dist_artifacts(received_dist_dir: Path) -> list[str]:
        assert received_dist_dir == dist_dir
        return [str(dist_dir / "package.whl"), str(dist_dir / "package.tar.gz")]

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_dist_artifacts", fake_dist_artifacts)

    assert module.main(["--skip-core", "--package", "--dist-dir", str(dist_dir)]) == 0

    assert calls[0] == (
        module.sys.executable,
        "-m",
        "build",
        "--outdir",
        str(dist_dir),
    )


def test_verify_local_rejects_explicit_nonempty_dist_dir(tmp_path) -> None:
    module = _load_script("scripts/verify_local.py")
    dist_dir = tmp_path / "release-dist"
    dist_dir.mkdir()
    (dist_dir / "stale.whl").write_text("", encoding="utf-8")

    assert module.main(["--skip-core", "--package", "--dist-dir", str(dist_dir)]) == 2


def test_verify_local_dist_dir_requires_package(tmp_path) -> None:
    module = _load_script("scripts/verify_local.py")

    assert module.main(["--dist-dir", str(tmp_path / "release-dist")]) == 2


def test_verify_local_package_checks_ignore_repository_dist_artifacts(
    tmp_path, monkeypatch
) -> None:
    module = _load_script("scripts/verify_local.py")
    stale_dist = tmp_path / "dist"
    stale_dist.mkdir()
    (stale_dist / "stale.whl").write_text("", encoding="utf-8")
    isolated_dist = tmp_path / "isolated-dist"
    isolated_dist.mkdir()
    built_wheel = isolated_dist / "package.whl"
    built_source = isolated_dist / "package.tar.gz"
    built_wheel.write_text("", encoding="utf-8")
    built_source.write_text("", encoding="utf-8")

    calls = list(
        module._commands(
            include_core=False,
            include_package=True,
            include_smoke=True,
            package_dist_dir=isolated_dist,
            wheel_smoke_venv_dir=tmp_path / "wheel-smoke",
        )
    )

    flattened_args = [arg for command in calls for arg in command]

    assert str(stale_dist / "stale.whl") not in flattened_args
    assert str(built_wheel) in flattened_args
    assert str(built_source) in flattened_args
    assert [module.sys.executable, "-m", "build", "--outdir", str(isolated_dist)] in calls


def test_verify_local_stops_on_first_failure(monkeypatch) -> None:
    module = _load_script("scripts/verify_local.py")
    calls: list[tuple[str, ...]] = []

    def fake_run(command: list[str], check: bool) -> object:
        calls.append(tuple(command))

        class Result:
            returncode = 7

        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main([]) == 7
    assert calls == [(module.sys.executable, "-m", "black", "--check", "infra", "tests", "scripts")]


def test_readme_documents_verify_local_entrypoint() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "python scripts/verify_local.py" in readme
    assert "python scripts/verify_local.py --package --smoke" in readme


def _load_script(path: str):
    spec = importlib.util.spec_from_file_location("verify_local", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
