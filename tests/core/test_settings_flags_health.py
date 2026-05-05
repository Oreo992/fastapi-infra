from infra.config.models import InfraSettings, PluginSettings
from infra.core.flags import FeatureFlag, resolve_feature_flag
from infra.core.health import HealthRegistry, HealthState, HealthStatus


def test_plugin_settings_default_to_auto():
    settings = PluginSettings()
    assert settings.enabled is None
    assert settings.config == {}


def test_infra_settings_reads_plugin_namespace():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {"default_provider": "mock"},
                }
            }
        }
    )
    plugin = settings.get_plugin("ai")
    assert plugin.enabled is True
    assert plugin.config == {"default_provider": "mock"}


def test_missing_plugin_uses_auto_settings():
    settings = InfraSettings()
    plugin = settings.get_plugin("payment")
    assert plugin.enabled is None
    assert plugin.config == {}


def test_feature_flag_resolution():
    assert resolve_feature_flag(True) is FeatureFlag.ENABLED
    assert resolve_feature_flag(False) is FeatureFlag.DISABLED
    assert resolve_feature_flag(None) is FeatureFlag.AUTO


def test_health_registry_tracks_disabled_and_healthy_statuses():
    registry = HealthRegistry()
    registry.set_status(HealthStatus(name="ai", status=HealthState.HEALTHY))
    registry.set_status(
        HealthStatus(name="payment", status=HealthState.DISABLED, message="disabled by config")
    )

    result = registry.snapshot()

    assert result["ai"].status is HealthState.HEALTHY
    assert result["payment"].status is HealthState.DISABLED
    assert result["payment"].message == "disabled by config"
