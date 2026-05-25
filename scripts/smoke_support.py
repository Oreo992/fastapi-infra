from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path


def install_editable_to_target(
    package_dir: Path,
    *,
    target_dir: Path,
    python: Path,
    timeout: float,
    run_command: Callable[..., None] | None = None,
    missing_message: str = "package was not generated",
) -> dict[str, str]:
    package_dir = package_dir.resolve()
    if not package_dir.joinpath("pyproject.toml").exists():
        raise FileNotFoundError(f"{missing_message}: {package_dir}")
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)

    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "-e",
        str(package_dir),
        "--no-deps",
        "--no-build-isolation",
        "--target",
        str(target_dir),
    ]
    runner = run_command or run
    runner(command, cwd=package_dir, timeout=timeout)
    return pythonpath_env(pythonpath_entries_for_target(target_dir))


def pythonpath_entries_for_target(target_dir: Path) -> list[Path]:
    entries = [target_dir.resolve()]
    for path_file in sorted(target_dir.glob("*.pth")):
        for raw_line in path_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("import "):
                continue
            path = Path(line)
            if not path.is_absolute():
                path = target_dir / path
            entries.append(path.resolve())
    return entries


def pythonpath_env(entries: list[Path]) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    pythonpath_entries = [str(entry) for entry in entries]
    if existing:
        pythonpath_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    return env


def run(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> None:
    print(f"+ {' '.join(command)}", flush=True)
    run_env = _subprocess_env(env)
    subprocess.run(command, cwd=cwd, check=True, timeout=timeout, env=run_env)


def _subprocess_env(env: dict[str, str] | None = None) -> dict[str, str]:
    run_env = dict(os.environ if env is None else env)
    python_bin = str(Path(sys.executable).resolve().parent)
    shim_bin = _ensure_fastapi_infra_shim()
    existing_path = run_env.get("PATH")
    path_entries = [shim_bin, python_bin]
    if existing_path:
        path_entries.append(existing_path)
    run_env["PATH"] = os.pathsep.join(path_entries)
    return run_env


def _ensure_fastapi_infra_shim() -> str:
    shim_dir = Path(tempfile.gettempdir()) / "fastapi-infra-smoke-bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "fastapi-infra"
    shim.write_text(
        "#!/bin/sh\n" f'exec {shlex.quote(sys.executable)} -m infra.cli "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return str(shim_dir)
