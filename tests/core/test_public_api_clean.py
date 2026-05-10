import infra
import pytest
import subprocess
import sys


def test_top_level_public_api_is_small_and_explicit():
    assert sorted(infra.__all__) == [
        "InfraContext",
        "InfraSettings",
        "PluginSettings",
        "setup_infra",
    ]


def test_importing_infra_has_no_legacy_settings_side_effects():
    result = subprocess.run(
        [sys.executable, "-c", "import infra"],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout == ""
    assert result.stderr == ""


def test_config_public_api_exposes_only_new_settings_models():
    import infra.config as config

    assert sorted(config.__all__) == ["InfraSettings", "PluginSettings"]
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
