from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
import tempfile
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

try:
    from infra.plugins.template import SUPPORTED_PROVIDER_KINDS
except ModuleNotFoundError:
    if __package__ not in (None, ""):
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from infra.plugins.template import SUPPORTED_PROVIDER_KINDS

_pythonpath_entries_for_target = smoke_support.pythonpath_entries_for_target
_pythonpath_env = smoke_support.pythonpath_env
_run = smoke_support.run

PROVIDER_EXAMPLES = {
    "ai": "openrouter",
    "notifications": "twilio",
    "payment": "adyen",
    "ratelimit": "upstash",
    "speech": "deepgram",
    "storage": "r2",
    "tasks": "nats",
    "webhook": "github",
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp())
    work_dir.mkdir(parents=True, exist_ok=True)
    python = Path(args.python)

    print(f"plugin template smoke work_dir={work_dir}", flush=True)
    _smoke_service_template(
        work_dir=work_dir,
        python=python,
        timeout=args.timeout,
        keep_existing=args.keep_existing,
    )
    for provider_kind in sorted(SUPPORTED_PROVIDER_KINDS):
        _smoke_provider_template(
            provider_kind,
            work_dir=work_dir,
            python=python,
            timeout=args.timeout,
            keep_existing=args.keep_existing,
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke_plugin_templates.py",
        description="Generate every external plugin/provider template and validate it.",
    )
    parser.add_argument(
        "--work-dir",
        help="directory where generated template packages should be written",
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
        help="reuse existing generated template directories instead of replacing them",
    )
    return parser


def _smoke_service_template(
    *,
    work_dir: Path,
    python: Path,
    timeout: float,
    keep_existing: bool,
) -> None:
    plugin_name = "template_service"
    package_dir = work_dir / plugin_name
    if package_dir.exists() and not keep_existing:
        shutil.rmtree(package_dir)

    print(f"== plugin template: {plugin_name} ==", flush=True)
    target_dir = work_dir / ".template-plugin-site" / plugin_name
    _run(
        [
            str(python),
            "-m",
            "infra.cli",
            "plugins",
            "init",
            plugin_name,
            str(package_dir),
            "--force",
        ],
        cwd=work_dir,
        timeout=timeout,
    )
    env = _install_editable(package_dir, target_dir=target_dir, python=python, timeout=timeout)
    _run([str(python), "-m", "pytest", "-q"], cwd=package_dir, timeout=timeout, env=env)
    _run(
        [
            str(python),
            "-m",
            "infra.cli",
            "plugins",
            "check",
            plugin_name,
            "--settings",
            "infra.example.toml",
            "--lifecycle",
        ],
        cwd=package_dir,
        timeout=timeout,
        env=env,
    )


def _smoke_provider_template(
    provider_kind: str,
    *,
    work_dir: Path,
    python: Path,
    timeout: float,
    keep_existing: bool,
) -> None:
    provider_name = PROVIDER_EXAMPLES[provider_kind]
    package_dir = work_dir / f"{provider_name}_{provider_kind}"
    if package_dir.exists() and not keep_existing:
        shutil.rmtree(package_dir)

    print(f"== provider template: {provider_kind}/{provider_name} ==", flush=True)
    target_dir = work_dir / ".template-plugin-site" / f"{provider_name}_{provider_kind}"
    _run(
        [
            str(python),
            "-m",
            "infra.cli",
            "plugins",
            "init",
            provider_name,
            str(package_dir),
            "--kind",
            "provider",
            "--provider-kind",
            provider_kind,
            "--force",
        ],
        cwd=work_dir,
        timeout=timeout,
    )
    env = _install_editable(package_dir, target_dir=target_dir, python=python, timeout=timeout)
    _run([str(python), "-m", "pytest", "-q"], cwd=package_dir, timeout=timeout, env=env)
    _run(
        [
            str(python),
            "-m",
            "infra.cli",
            "config-check",
            "--settings",
            "infra.example.toml",
        ],
        cwd=package_dir,
        timeout=timeout,
        env=env,
    )


def _install_editable(
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
            missing_message="template package was not generated",
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
