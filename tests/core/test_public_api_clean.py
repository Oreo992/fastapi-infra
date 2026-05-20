import importlib.util
import subprocess
import sys

import pytest

import infra
import infra.plugins


def test_top_level_public_api_is_small_and_explicit():
    assert sorted(infra.__all__) == [
        "InfraContext",
        "InfraSettings",
        "PluginSettings",
        "ServiceKey",
        "get_infra",
        "infra_service",
        "setup_infra",
    ]


def test_plugins_public_api_exports_service_keys():
    assert sorted(infra.plugins.__all__) == [
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
        "PluginConfigValidatorHook",
        "PluginContext",
        "PluginDependencyError",
        "PluginManager",
        "PluginManifestHintsHook",
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


def test_importing_infra_has_no_legacy_settings_side_effects():
    code = """
import sys

import infra

eager_modules = {
    "infra.plugins.ai",
    "infra.plugins.auth",
    "infra.plugins.payment",
    "infra.plugins.speech",
    "infra.plugins.storage",
}
loaded = sorted(eager_modules & set(sys.modules))
assert loaded == [], loaded
assert infra.setup_infra is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout == ""
    assert result.stderr == ""


def test_plugin_contract_import_does_not_eager_load_builtin_plugins():
    code = """
import sys

import infra.plugins.contract

eager_modules = {
    "infra.plugins.ai",
    "infra.plugins.auth",
    "infra.plugins.payment",
    "infra.plugins.speech",
    "infra.plugins.storage",
}
loaded = sorted(eager_modules & set(sys.modules))
assert loaded == [], loaded

from infra.plugins import get_builtin_plugins

plugins = get_builtin_plugins()
assert plugins
assert "infra.plugins.ai" in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout == ""
    assert result.stderr == ""


def test_tooling_modules_do_not_eager_load_builtin_plugins():
    code = """
import sys

import infra.cli
import infra.release_check
import infra.scaffold

eager_modules = {
    "infra.plugins.ai",
    "infra.plugins.auth",
    "infra.plugins.payment",
    "infra.plugins.speech",
    "infra.plugins.storage",
}
loaded = sorted(eager_modules & set(sys.modules))
assert loaded == [], loaded
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout == ""
    assert result.stderr == ""


def test_core_imports_do_not_require_optional_integration_dependencies():
    code = """
import builtins
import inspect
from pathlib import Path

blocked = {"aiomysql", "aiohttp", "orjson", "redis"}
repo_infra = Path.cwd() / "infra"
real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    caller = inspect.currentframe().f_back
    caller_path = Path(caller.f_code.co_filename) if caller is not None else None
    if (
        name.split(".", 1)[0] in blocked
        and caller_path is not None
        and caller_path.is_relative_to(repo_infra)
    ):
        raise ImportError(name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import

import infra
import infra.cache.service
import infra.database
import infra.database.manager
import infra.database.repository
import infra.http
import infra.plugins.services
import infra.streaming.streams_manager
from infra.plugins.tasks import TasksPlugin

assert TasksPlugin is not None
assert infra.plugins.services.PAYMENT_SERVICE.name == "payment"
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout == ""
    assert result.stderr == ""


def test_provider_plugin_imports_do_not_require_optional_provider_sdks():
    code = """
import builtins
import inspect
from pathlib import Path

blocked = {"aiomysql", "anthropic", "google", "openai", "redis"}
repo_infra = Path.cwd() / "infra"
real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    caller = inspect.currentframe().f_back
    caller_path = Path(caller.f_code.co_filename) if caller is not None else None
    if (
        name.split(".", 1)[0] in blocked
        and caller_path is not None
        and caller_path.is_relative_to(repo_infra)
    ):
        raise ImportError(name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import

import infra.plugins.ai.plugin
import infra.plugins.payment.plugin
import infra.plugins.speech.plugin
import infra.plugins.storage.local

assert infra.plugins.ai.plugin.AIPlugin is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout == ""
    assert result.stderr == ""


def test_config_public_api_exposes_new_settings_helpers():
    import infra.config as config

    assert sorted(config.__all__) == [
        "InfraConfigValidationIssue",
        "InfraSettings",
        "PluginSettings",
        "load_env_file",
        "load_infra_settings",
        "validate_infra_settings",
    ]
    assert not hasattr(config, "BaseSettings")
    assert not hasattr(config, "get_platform_env_file")


def test_database_manager_module_has_no_legacy_global_helpers():
    import infra.database.manager as database_manager

    for name in [
        "db_manager",
        "init_database",
        "close_database",
        "check_database_health",
        "get_db_connection",
        "get_redis",
        "get_db_session",
    ]:
        assert not hasattr(database_manager, name)


def test_database_manager_instances_are_not_process_wide_singletons():
    from infra.database.manager import DatabaseManager

    first = DatabaseManager({"mysql_host": "db-a"})
    second = DatabaseManager({"mysql_host": "db-b"})

    assert first is not second


def test_database_consumers_require_explicit_database_manager():
    from infra.database.repository import UnitOfWork
    from infra.plugins.lock.manager import DistributedLockManager
    from infra.streaming.streams_manager import StreamConfig, StreamsManager

    for factory in [
        UnitOfWork,
        DistributedLockManager,
        lambda: StreamsManager(StreamConfig(stream_name="events")),
    ]:
        with pytest.raises(TypeError):
            factory()


def test_removed_legacy_runtime_modules_are_not_importable():
    for module_name in [
        "infra.registry",
        "infra.startup",
        "infra.concurrency",
    ]:
        assert importlib.util.find_spec(module_name) is None


def test_http_module_exposes_explicit_client_without_process_wide_helpers():
    import infra.http as http

    assert sorted(http.__all__) == [
        "HttpClient",
        "HttpResponse",
        "HttpRetryConfig",
        "MockHttpClient",
        "PresetConfigs",
        "RetryConfig",
        "TimeoutConfig",
        "with_resilience",
    ]
    for name in [
        "HttpClientManager",
        "get",
        "post",
        "put",
        "delete",
        "resilience_manager",
    ]:
        assert not hasattr(http, name)


def test_logging_module_requires_explicit_setup_without_global_manager():
    import infra.logging as logging_api

    assert "setup_logging" in logging_api.__all__
    assert "log_manager" not in logging_api.__all__
    assert not hasattr(logging_api, "log_manager")
