from __future__ import annotations

import os
import shutil
import subprocess
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
    subprocess.run(command, cwd=cwd, check=True, timeout=timeout, env=env)
