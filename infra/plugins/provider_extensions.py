from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import entry_points
from typing import Any

ProviderFactory = Callable[[Mapping[str, Any]], Any]


def entry_point_provider_names(group: str) -> set[str]:
    return {entry_point.name for entry_point in entry_points(group=group)}


def external_provider_names_to_load(
    *,
    provider_kind: str,
    requested_names: set[str],
    registered_names: set[str],
    entry_point_group: str,
) -> list[str]:
    external_names = entry_point_provider_names(entry_point_group)
    unknown_names = requested_names - registered_names - external_names
    if unknown_names:
        raise ValueError(f"unknown {provider_kind} provider: {', '.join(sorted(unknown_names))}")
    return sorted(requested_names - registered_names)


def load_entry_point_provider(
    group: str,
    provider_name: str,
    config: Mapping[str, Any],
    *,
    required_methods: Sequence[str] = (),
) -> Any:
    for entry_point in entry_points(group=group):
        if entry_point.name != provider_name:
            continue
        factory = entry_point.load()
        if not callable(factory):
            raise ValueError(f"{group}:{provider_name} must load a provider factory")
        provider = factory(config)
        actual_name = getattr(provider, "name", None)
        if actual_name != provider_name:
            raise ValueError(f"{group}:{provider_name} returned provider named {actual_name!r}")
        _validate_provider_methods(group, provider_name, provider, required_methods)
        return provider
    raise LookupError(f"unknown provider entry point: {group}:{provider_name}")


def _validate_provider_methods(
    group: str,
    provider_name: str,
    provider: Any,
    required_methods: Sequence[str],
) -> None:
    missing = [
        method_name
        for method_name in required_methods
        if not callable(getattr(provider, method_name, None))
    ]
    if missing:
        raise ValueError(
            f"{group}:{provider_name} provider is missing required method(s): " + ", ".join(missing)
        )
