from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

Command = list[str]

BASE_WHEEL_SMOKE = """
import builtins
import inspect
import os
import site
from pathlib import Path

os.chdir("/tmp")

blocked = {"aiomysql", "anthropic", "google", "openai", "redis"}
site_roots = [Path(path).resolve() for path in site.getsitepackages()]
real_import = builtins.__import__


def guarded_import(name, *args, **kwargs):
    caller = inspect.currentframe().f_back
    caller_path = Path(caller.f_code.co_filename).resolve() if caller is not None else None
    if (
        name.split(".", 1)[0] in blocked
        and caller_path is not None
        and any(caller_path.is_relative_to(root / "infra") for root in site_roots)
    ):
        raise ImportError(name)
    return real_import(name, *args, **kwargs)


builtins.__import__ = guarded_import

import infra
import infra.plugins.ai.plugin
import infra.plugins.payment.plugin
import infra.plugins.speech.plugin
import infra.plugins.storage.local

assert sorted(infra.__all__) == [
    "InfraContext",
    "InfraSettings",
    "PluginSettings",
    "ServiceKey",
    "get_infra",
    "infra_service",
    "setup_infra",
]
assert infra.plugins.ai.plugin.AIPlugin is not None
"""

OBSERVABILITY_WHEEL_SMOKE = """
import os

os.chdir("/tmp")

from infra.core.health import HealthRegistry
from infra.plugins.observability import ObservabilityService

service = ObservabilityService(
    HealthRegistry(),
    metrics_backend="prometheus",
    tracing_backend="opentelemetry",
)
service.increment("requests_total")
with service.span("ci.observability"):
    service.event("inside-span")

metrics = service.render_metrics() or ""
assert "# TYPE requests_total counter" in metrics
assert service.events[0].name == "inside-span"
"""


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.dist_dir is not None and not args.package:
        print("--dist-dir requires --package", file=sys.stderr)
        return 2

    package_dist_context: AbstractContextManager[str | Path | None]
    if args.package and args.dist_dir is not None:
        package_dist_dir = args.dist_dir
        package_dist_dir.mkdir(parents=True, exist_ok=True)
        existing_artifacts = sorted(package_dist_dir.iterdir())
        if existing_artifacts:
            joined = ", ".join(str(path) for path in existing_artifacts[:3])
            if len(existing_artifacts) > 3:
                joined = f"{joined}, ..."
            print(
                f"--dist-dir must be empty before building package artifacts: {joined}",
                file=sys.stderr,
            )
            return 2
        package_dist_context = nullcontext(package_dist_dir)
    else:
        package_dist_context = (
            tempfile.TemporaryDirectory(prefix="fastapi-infra-dist-")
            if args.package
            else nullcontext(None)
        )
    wheel_smoke_dir_context = (
        tempfile.TemporaryDirectory(prefix="fastapi-infra-wheel-smoke-")
        if args.package and args.smoke
        else nullcontext(None)
    )
    with package_dist_context as package_dist_dir, wheel_smoke_dir_context as wheel_smoke_dir:
        for command in _commands(
            include_core=not args.skip_core,
            include_package=args.package,
            include_smoke=args.smoke,
            package_dist_dir=Path(package_dist_dir) if package_dist_dir else None,
            wheel_smoke_venv_dir=Path(wheel_smoke_dir) if wheel_smoke_dir else None,
        ):
            print(f"+ {' '.join(command)}", flush=True)
            result = subprocess.run(command, check=False)
            if result.returncode != 0:
                return result.returncode
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_local.py",
        description="Run the local CI verification gates for fastapi-infra.",
    )
    parser.add_argument(
        "--package",
        action="store_true",
        help="also build distributions and verify package metadata/content",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="also run generated-project and plugin-template smoke checks",
    )
    parser.add_argument(
        "--skip-core",
        action="store_true",
        help="skip formatting, type checking, and unit tests; useful for CI package jobs",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        help=(
            "write verified package artifacts to this empty directory instead of "
            "using a temporary package build directory; requires --package"
        ),
    )
    return parser


def _commands(
    *,
    include_core: bool,
    include_package: bool,
    include_smoke: bool,
    package_dist_dir: Path | None = None,
    wheel_smoke_venv_dir: Path | None = None,
) -> Iterable[Command]:
    python = sys.executable
    package_artifacts: list[str] = []
    if include_core:
        yield [python, "-m", "black", "--check", "infra", "tests", "scripts"]
        yield [python, "-m", "isort", "--check-only", "infra", "tests", "scripts"]
        yield [python, "scripts/typecheck.py"]
        yield [python, "-m", "pytest", "tests", "-q"]
    if include_package:
        package_dist_dir = package_dist_dir or Path("dist")
        yield [python, "-m", "build", "--outdir", str(package_dist_dir)]
        package_artifacts = _dist_artifacts(package_dist_dir)
        yield [python, "-m", "twine", "check", *package_artifacts]
        yield [python, "scripts/check_distribution.py", *package_artifacts]
    if include_smoke:
        smoke_python = python
        if wheel_smoke_venv_dir is not None:
            wheel = _wheel_artifact(package_artifacts)
            yield from _base_wheel_smoke_commands(
                wheel,
                wheel_smoke_venv_dir / "base",
                python=python,
            )
            smoke_python = str(_venv_python(wheel_smoke_venv_dir / "dev"))
            yield from _wheel_venv_install_commands(
                f"{wheel}[dev]",
                wheel_smoke_venv_dir / "dev",
                python=python,
                smoke_python=smoke_python,
                with_setuptools=True,
            )
        yield [
            smoke_python,
            "scripts/smoke_generated_projects.py",
            "--work-dir",
            "/tmp/fastapi-infra-generated-smoke",
            "--external-plugin-example",
            "examples/search_plugin",
        ]
        yield [
            smoke_python,
            "scripts/smoke_plugin_templates.py",
            "--work-dir",
            "/tmp/fastapi-infra-plugin-template-smoke",
        ]
        if wheel_smoke_venv_dir is not None:
            yield from _observability_wheel_smoke_commands(
                wheel,
                wheel_smoke_venv_dir / "observability",
                python=python,
            )


def _dist_artifacts(dist_dir: Path) -> list[str]:
    return [str(artifact) for artifact in sorted(dist_dir.glob("*"))]


def _wheel_artifact(artifacts: Sequence[str]) -> str:
    wheels = sorted(artifact for artifact in artifacts if artifact.endswith(".whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            "expected exactly one built wheel artifact before wheel smoke; " f"found {len(wheels)}"
        )
    return str(wheels[0])


def _base_wheel_smoke_commands(
    wheel: str,
    venv_dir: Path,
    *,
    python: str,
) -> Iterable[Command]:
    smoke_python = str(_venv_python(venv_dir))
    yield from _wheel_venv_install_commands(
        wheel,
        venv_dir,
        python=python,
        smoke_python=smoke_python,
    )
    yield [smoke_python, "-c", BASE_WHEEL_SMOKE]


def _observability_wheel_smoke_commands(
    wheel: str,
    venv_dir: Path,
    *,
    python: str,
) -> Iterable[Command]:
    smoke_python = str(_venv_python(venv_dir))
    yield from _wheel_venv_install_commands(
        f"{wheel}[observability]",
        venv_dir,
        python=python,
        smoke_python=smoke_python,
    )
    yield [smoke_python, "-c", OBSERVABILITY_WHEEL_SMOKE]


def _wheel_venv_install_commands(
    requirement: str,
    venv_dir: Path,
    *,
    python: str,
    smoke_python: str,
    with_setuptools: bool = False,
) -> Iterable[Command]:
    yield [python, "-m", "venv", str(venv_dir)]
    bootstrap_packages = ["pip"]
    if with_setuptools:
        bootstrap_packages.append("setuptools")
    yield [smoke_python, "-m", "pip", "install", "--upgrade", *bootstrap_packages]
    yield [smoke_python, "-m", "pip", "install", requirement]


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


if __name__ == "__main__":
    raise SystemExit(main())
