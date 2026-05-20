from importlib import import_module
from typing import Any

from infra.plugins.contract import (
    InfraPlugin,
    PluginConfigValidatorHook,
    PluginContext,
    PluginManifestHintsHook,
    PluginMetadata,
    PluginProviderCertificationHook,
    PluginProviderPolicyHook,
    PluginReleaseCheckHook,
    PluginReleaseDependencyHook,
)
from infra.plugins.manager import PluginDependencyError, PluginManager
from infra.plugins.services import (
    AI_SERVICE,
    AUTH_SERVICE,
    CACHE_SERVICE,
    DATABASE_SERVICE,
    HTTP_SERVICE,
    NOTIFICATIONS_SERVICE,
    OBSERVABILITY_SERVICE,
    PAYMENT_SERVICE,
    RATELIMIT_SERVICE,
    SPEECH_SERVICE,
    STORAGE_SERVICE,
    TASKS_SERVICE,
    WEBHOOKS_SERVICE,
    NotificationService,
    RateLimiterService,
    StorageService,
)

_LAZY_EXPORTS = {
    "get_builtin_plugins": ("infra.plugins.builtin", "get_builtin_plugins"),
}

__all__ = [
    "AI_SERVICE",
    "AUTH_SERVICE",
    "CACHE_SERVICE",
    "DATABASE_SERVICE",
    "HTTP_SERVICE",
    "InfraPlugin",
    "NOTIFICATIONS_SERVICE",
    "NotificationService",
    "OBSERVABILITY_SERVICE",
    "PAYMENT_SERVICE",
    "PluginContext",
    "PluginConfigValidatorHook",
    "PluginDependencyError",
    "PluginManifestHintsHook",
    "PluginManager",
    "PluginMetadata",
    "PluginProviderCertificationHook",
    "PluginProviderPolicyHook",
    "PluginReleaseCheckHook",
    "PluginReleaseDependencyHook",
    "RATELIMIT_SERVICE",
    "RateLimiterService",
    "SPEECH_SERVICE",
    "STORAGE_SERVICE",
    "StorageService",
    "TASKS_SERVICE",
    "WEBHOOKS_SERVICE",
    "get_builtin_plugins",
]


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = _LAZY_EXPORTS[name]
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
