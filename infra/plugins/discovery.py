from collections.abc import Callable, Iterable
from importlib.metadata import entry_points
from typing import Any, cast

from infra.config.models import InfraSettings
from infra.plugins.contract import InfraPlugin
from infra.plugins.manager import PluginDependencyError

PLUGIN_ENTRY_POINT_GROUP = "fastapi_infra.plugins"


def get_available_plugins(settings: InfraSettings | None = None) -> list[InfraPlugin]:
    from infra.plugins.builtin import get_builtin_plugins

    builtin_plugins = get_builtin_plugins()
    builtin_names = {plugin.metadata.name for plugin in builtin_plugins}
    configured_external_names = None
    if settings is not None:
        configured_external_names = set(settings.infra.plugins) - builtin_names
    return [
        *builtin_plugins,
        *load_entry_point_plugins(configured_names=configured_external_names),
    ]


def load_entry_point_plugins(
    *,
    configured_names: set[str] | None = None,
    group: str = PLUGIN_ENTRY_POINT_GROUP,
    entry_points_loader: Callable[..., Iterable[Any]] | None = None,
) -> list[InfraPlugin]:
    loader = entry_points_loader or entry_points
    plugins: list[InfraPlugin] = []
    for entry_point in loader(group=group):
        name = getattr(entry_point, "name", None)
        if configured_names is not None and name not in configured_names:
            continue
        plugins.append(_load_entry_point_plugin(entry_point))
    return plugins


def _load_entry_point_plugin(entry_point: Any) -> InfraPlugin:
    loaded = entry_point.load()
    candidate = loaded() if isinstance(loaded, type) else loaded
    if not hasattr(candidate, "metadata") and callable(candidate):
        candidate = candidate()
    if not _is_plugin_like(candidate):
        name = getattr(entry_point, "name", "<unknown>")
        raise PluginDependencyError(f"entry point plugin {name!r} does not implement InfraPlugin")
    entry_point_name = getattr(entry_point, "name", None)
    if entry_point_name and candidate.metadata.name != entry_point_name:
        raise PluginDependencyError(
            f"entry point plugin {entry_point_name!r} returned plugin {candidate.metadata.name!r}"
        )
    return cast(InfraPlugin, candidate)


def _is_plugin_like(candidate: Any) -> bool:
    metadata = getattr(candidate, "metadata", None)
    if metadata is None or not getattr(metadata, "name", None):
        return False
    return all(
        callable(getattr(candidate, method_name, None))
        for method_name in ("register", "startup", "shutdown", "health_check")
    )
