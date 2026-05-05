import infra
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
