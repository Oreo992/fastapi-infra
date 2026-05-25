from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Sequence, cast

if __package__ in (None, ""):
    spec = importlib.util.spec_from_file_location(
        "_fastapi_infra_smoke_support",
        Path(__file__).with_name("smoke_support.py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load smoke_support.py")
    smoke_support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke_support)
else:
    from scripts import smoke_support

_pythonpath_entries_for_target = smoke_support.pythonpath_entries_for_target
_pythonpath_env = smoke_support.pythonpath_env
_run = smoke_support.run

DEFAULT_PROFILES = ("minimal", "api", "saas")
KNOWN_PROFILES = ("minimal", "api", "worker", "ai", "saas", "full")
CI_ENV_OVERRIDES = {
    "JWT_SECRET": "ci-jwt-secret-at-least-32-characters-long",
    "SEARCH_API_KEY": "ci-search-api-key",
}
PROVIDER_ENV_PLACEHOLDER_PREFIX = "ci-provider"


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp())
    work_dir.mkdir(parents=True, exist_ok=True)
    profiles = tuple(args.profile or DEFAULT_PROFILES)

    print(f"generated project smoke work_dir={work_dir}", flush=True)
    for profile in profiles:
        _smoke_profile(
            profile,
            work_dir=work_dir,
            python=Path(args.python),
            timeout=args.timeout,
            keep_existing=args.keep_existing,
        )
    if args.external_plugin_example is not None:
        plugin_env = _install_editable_plugin(
            Path(args.external_plugin_example),
            target_dir=work_dir / ".external-plugin-site",
            python=Path(args.python),
            timeout=args.timeout,
        )
        _smoke_external_plugin(
            "search",
            work_dir=work_dir,
            python=Path(args.python),
            timeout=args.timeout,
            keep_existing=args.keep_existing,
            env=plugin_env,
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke_generated_projects.py",
        description="Generate scaffold profiles and run their local and static release gates.",
    )
    parser.add_argument(
        "--profile",
        action="append",
        choices=KNOWN_PROFILES,
        help="profile to smoke test; can be repeated; defaults to minimal, api, and saas",
    )
    parser.add_argument(
        "--work-dir",
        help="directory where generated projects should be written",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable with fastapi-infra installed",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="timeout in seconds for each subprocess command",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="reuse existing generated project directories instead of replacing them",
    )
    parser.add_argument(
        "--external-plugin-example",
        type=Path,
        help="editable external plugin package to install and smoke through fastapi-infra new",
    )
    return parser


def _smoke_profile(
    profile: str,
    *,
    work_dir: Path,
    python: Path,
    timeout: float,
    keep_existing: bool,
) -> None:
    project_dir = work_dir / f"{profile}_app"
    if project_dir.exists() and not keep_existing:
        shutil.rmtree(project_dir)

    print(f"== profile: {profile} ==", flush=True)
    if not project_dir.exists():
        _run(
            [str(python), "-m", "infra.cli", "new", str(project_dir), "--profile", profile],
            cwd=work_dir,
            timeout=timeout,
        )
    _install_generated_project_dependencies(project_dir, python=python, timeout=timeout)
    _run(["make", "env"], cwd=project_dir, timeout=timeout)
    _write_ci_env(project_dir / ".env", project_dir / ".env")
    _write_ci_env(
        project_dir / "provider.env",
        project_dir / "provider.env",
        blank_value_prefix=PROVIDER_ENV_PLACEHOLDER_PREFIX,
    )
    _run(["make", "verify"], cwd=project_dir, timeout=timeout)
    _run(["make", "release-static"], cwd=project_dir, timeout=timeout)
    _run(["scripts/verify-release.sh", ".env", "provider.env"], cwd=project_dir, timeout=timeout)


def _install_editable_plugin(
    package_dir: Path,
    *,
    target_dir: Path,
    python: Path,
    timeout: float,
) -> dict[str, str]:
    return cast(
        dict[str, str],
        smoke_support.install_editable_to_target(
            package_dir,
            target_dir=target_dir,
            python=python,
            timeout=timeout,
            run_command=_run,
            missing_message="external plugin package not found",
        ),
    )


def _smoke_external_plugin(
    plugin_name: str,
    *,
    work_dir: Path,
    python: Path,
    timeout: float,
    keep_existing: bool,
    env: dict[str, str],
) -> None:
    project_dir = work_dir / f"{plugin_name}_plugin_app"
    if project_dir.exists() and not keep_existing:
        shutil.rmtree(project_dir)

    print(f"== external plugin: {plugin_name} ==", flush=True)
    if not project_dir.exists():
        _run(
            [
                str(python),
                "-m",
                "infra.cli",
                "new",
                str(project_dir),
                "--plugins",
                plugin_name,
            ],
            cwd=work_dir,
            timeout=timeout,
            env=env,
        )
    _install_generated_project_dependencies(project_dir, python=python, timeout=timeout, env=env)
    _run(["make", "env"], cwd=project_dir, timeout=timeout, env=env)
    _write_ci_env(project_dir / ".env", project_dir / ".env")
    _write_ci_env(
        project_dir / "provider.env",
        project_dir / "provider.env",
        blank_value_prefix=PROVIDER_ENV_PLACEHOLDER_PREFIX,
    )
    _run(
        [str(python), "-m", "infra.cli", "plugins", "check", plugin_name, "--lifecycle"],
        cwd=project_dir,
        timeout=timeout,
        env=env,
    )
    _run(["make", "verify"], cwd=project_dir, timeout=timeout, env=env)
    _run(["make", "release-static"], cwd=project_dir, timeout=timeout, env=env)
    _run(
        ["scripts/verify-release.sh", ".env", "provider.env"],
        cwd=project_dir,
        timeout=timeout,
        env=env,
    )


def _install_generated_project_dependencies(
    project_dir: Path,
    *,
    python: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> None:
    dependencies = _read_project_dependencies(project_dir / "pyproject.toml")
    if not dependencies:
        return
    command = [str(python), "-m", "pip", "install", *dependencies]
    if env is None:
        _run(command, cwd=project_dir, timeout=timeout)
        return
    _run(command, cwd=project_dir, timeout=timeout, env=env)


def _read_project_dependencies(pyproject_path: Path) -> list[str]:
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data.get("project")
    if not isinstance(project, Mapping):
        return []
    dependencies = _string_list(project.get("dependencies"))
    optional_dependencies = project.get("optional-dependencies")
    if isinstance(optional_dependencies, Mapping):
        dependencies.extend(_string_list(optional_dependencies.get("dev")))
    return dependencies


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _write_ci_env(
    source: Path,
    target: Path,
    *,
    blank_value_prefix: str | None = None,
) -> None:
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            lines.append(line)
            continue
        name, _, value = line.partition("=")
        replacement = CI_ENV_OVERRIDES.get(name, value)
        if blank_value_prefix is not None and not replacement:
            replacement = f"{blank_value_prefix}-{name.lower()}"
        lines.append(f"{name}={replacement}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
