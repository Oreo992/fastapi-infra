from importlib import import_module
from typing import Any

from infra.core.context import InfraContext
from infra.core.dependencies import get_infra, infra_service
from infra.core.flags import FeatureFlag, resolve_feature_flag
from infra.core.health import HealthRegistry, HealthState, HealthStatus
from infra.core.services import ServiceKey

_LAZY_EXPORTS = {
    "setup_infra": ("infra.core.app", "setup_infra"),
}

__all__ = [
    "FeatureFlag",
    "get_infra",
    "HealthRegistry",
    "HealthState",
    "HealthStatus",
    "InfraContext",
    "infra_service",
    "resolve_feature_flag",
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
