import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from infra.config.models import InfraSettings

MissingEnvPolicy = Literal["error", "placeholder"]


def load_infra_settings(
    path: str | Path | None = None,
    env_prefix: str = "INFRA",
    *,
    missing_env: MissingEnvPolicy = "error",
) -> InfraSettings:
    data: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        if config_path.exists():
            data = _read_config_file(config_path)

    _deep_merge(data, _read_env_settings(env_prefix))
    data = _resolve_env_references(data, missing_env=missing_env)
    return InfraSettings(**data)


def load_env_file(
    path: str | Path,
    *,
    base_environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(base_environ if base_environ is not None else os.environ)
    file_path = Path(path)
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"env file could not be read: {file_path}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        try:
            parsed = _parse_env_line(raw_line)
        except ValueError as exc:
            raise ValueError(f"env file line {line_number}: {exc}") from exc
        if parsed is None:
            continue
        key, value = parsed
        env[key] = value
    return env


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


def _resolve_env_references(value: Any, *, missing_env: MissingEnvPolicy) -> Any:
    if isinstance(value, list):
        return [_resolve_env_references(item, missing_env=missing_env) for item in value]
    if isinstance(value, dict):
        if "$env" in value:
            if set(value) != {"$env"}:
                raise ValueError("Env reference objects must only contain '$env'")
            variable = value["$env"]
            if not isinstance(variable, str) or not variable:
                raise ValueError("Env reference '$env' value must be a non-empty string")
            if variable not in os.environ:
                if missing_env == "placeholder":
                    return _env_reference_placeholder(variable)
                raise ValueError(f"Required environment variable is not set: {variable}")
            return os.environ[variable]
        return {
            key: _resolve_env_references(item, missing_env=missing_env)
            for key, item in value.items()
        }
    return value


def _env_reference_placeholder(variable: str) -> str:
    if variable.endswith("_PORT"):
        return "1"
    if variable.endswith("_URL"):
        if "REDIS" in variable:
            return "redis://localhost:6379/0"
        return "https://example.test"
    if variable.endswith("_REGION"):
        return "us-east-1"
    if variable.endswith("_HOST"):
        return "localhost"
    if variable.endswith("_DATABASE") or variable.endswith("_DB"):
        return "app"
    if variable.endswith("_SENDER"):
        return "noreply@example.test"
    if variable.endswith("_RECIPIENT"):
        return "ops@example.test"
    if variable.endswith("_SECRET") or variable.endswith("_PASSWORD"):
        return "placeholder-secret-value"
    if variable.endswith("_API_KEY"):
        return "placeholder-api-key"
    return "placeholder"


def _parse_env_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    if "=" not in line:
        raise ValueError(f"line is not KEY=VALUE: {raw_line}")

    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not _valid_env_key(key):
        raise ValueError(f"invalid key: {key!r}")
    return key, _parse_env_value(value)


def _valid_env_key(key: str) -> bool:
    return (
        bool(key)
        and key.isascii()
        and (key[0].isalpha() or key[0] == "_")
        and all(char.isalnum() or char == "_" for char in key)
    )


def _parse_env_value(value: str) -> str:
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        closing_index = _closing_quote_index(value, quote)
        if closing_index is None:
            raise ValueError("env file has an unterminated quoted value")
        suffix = value[closing_index + 1 :].strip()
        if suffix and not suffix.startswith("#"):
            raise ValueError("env file has unexpected content after quoted value")
        inner = value[1:closing_index]
        if quote == '"':
            return _decode_double_quoted_env_value(inner)
        return inner
    return _strip_unquoted_comment(value).strip()


def _closing_quote_index(value: str, quote: str) -> int | None:
    for index in range(1, len(value)):
        if value[index] != quote:
            continue
        if quote == "'" or not _escaped_by_backslash(value, index):
            return index
    return None


def _escaped_by_backslash(value: str, index: int) -> bool:
    slash_count = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        slash_count += 1
        cursor -= 1
    return slash_count % 2 == 1


def _decode_double_quoted_env_value(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\" or index == len(value) - 1:
            result.append(char)
            index += 1
            continue

        escaped = value[index + 1]
        if escaped == "n":
            result.append("\n")
        elif escaped == "r":
            result.append("\r")
        elif escaped == "t":
            result.append("\t")
        elif escaped in {'"', "\\", "$"}:
            result.append(escaped)
        else:
            result.append("\\")
            result.append(escaped)
        index += 2
    return "".join(result)


def _strip_unquoted_comment(value: str) -> str:
    for index, char in enumerate(value):
        if char != "#":
            continue
        if index == 0 or value[index - 1].isspace():
            return value[:index]
    return value


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        else:
            target[key] = value
