from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence

CHECK_PATHS = (
    "infra",
    "scripts",
    "tests",
)


def main(argv: Sequence[str] | None = None) -> int:
    extra_args = tuple(argv or ())
    command = [sys.executable, "-m", "mypy", *extra_args, *CHECK_PATHS]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
