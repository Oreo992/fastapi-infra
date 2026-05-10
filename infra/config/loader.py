import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from infra.config.models import InfraSettings


def load_infra_settings(
    path: str | Path | None = None, env_prefix: str = "INFRA"
) -> InfraSettings:
    data: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        if config_path.exists():
            data = _read_config_file(config_path)

    _deep_merge(data, _read_env_settings(env_prefix))
    return InfraSettings(**data)


def _read_config_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    elif suffix == ".toml":
        with path.open("rb") as file:
            data = tomllib.load(file)
    else:
        raise ValueError(f"Unsupported config extension: {path.suffix}")

    if not isinstance(data, dict):
        raise ValueError("Infra config root must be an object")
    return data


def _read_env_settings(env_prefix: str) -> dict[str, Any]:
    prefix = f"{env_prefix}__"
    settings: dict[str, Any] = {}
    for name, value in os.environ.items():
        if not name.startswith(prefix):
            continue

        path = [part.lower() for part in name[len(prefix) :].split("__") if part]
        if not path:
            continue
        _set_nested(settings, path, _coerce_env_value(value))
    return settings


def _set_nested(target: dict[str, Any], path: list[str], value: Any) -> None:
    current = target
    for part in path[:-1]:
        existing = current.get(part)
        if not isinstance(existing, dict):
            existing = {}
            current[part] = existing
        current = existing
    current[path[-1]] = value


def _coerce_env_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        else:
            target[key] = value
