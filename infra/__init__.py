from importlib import import_module
from typing import Any

from infra.config.models import InfraSettings, PluginSettings
from infra.core.context import InfraContext
from infra.core.dependencies import get_infra, infra_service
from infra.core.services import ServiceKey

__version__ = "0.2.0"

_LAZY_EXPORTS = {
    "setup_infra": ("infra.core.app", "setup_infra"),
}

__all__ = [
    "get_infra",
    "InfraContext",
    "InfraSettings",
    "infra_service",
    "PluginSettings",
    "ServiceKey",
    "setup_infra",
]


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = _LAZY_EXPORTS[name]
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
