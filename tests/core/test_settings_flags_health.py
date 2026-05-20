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


def test_health_registry_is_not_affected_by_mutating_original_status():
    registry = HealthRegistry()
    status = HealthStatus(
        name="ai",
        status=HealthState.HEALTHY,
        details={"checks": {"database": "ok"}},
    )

    registry.set_status(status)
    status.status = HealthState.UNHEALTHY
    status.details["checks"]["database"] = "failed"

    result = registry.snapshot()

    assert result["ai"].status is HealthState.HEALTHY
    assert result["ai"].details == {"checks": {"database": "ok"}}


def test_health_registry_snapshot_returns_independent_status_models():
    registry = HealthRegistry()
    registry.set_status(
        HealthStatus(
            name="ai",
            status=HealthState.HEALTHY,
            details={"checks": {"database": "ok"}},
        )
    )

    first_snapshot = registry.snapshot()
    first_snapshot["ai"].status = HealthState.DEGRADED
    first_snapshot["ai"].details["checks"]["database"] = "slow"

    second_snapshot = registry.snapshot()

    assert second_snapshot["ai"].status is HealthState.HEALTHY
    assert second_snapshot["ai"].details == {"checks": {"database": "ok"}}


def test_health_status_redacts_secret_values_from_messages_and_details():
    status = HealthStatus(
        name="provider",
        status=HealthState.UNHEALTHY,
        message="probe failed api_key=sk_live_123 Authorization: Bearer token-123",
        details={
            "api_key": "sk_live_123",
            "password": "secret-password",
            "nested": {
                "secret_access_key": "aws-secret",
                "host": "smtp.example.test",
            },
            "items": [{"token": "token-123"}],
            "account_id": "acct_123",
        },
    )

    assert "sk_live_123" not in status.message
    assert "token-123" not in status.message
    assert status.details == {
        "api_key": "[redacted]",
        "password": "[redacted]",
        "nested": {
            "secret_access_key": "[redacted]",
            "host": "smtp.example.test",
        },
        "items": [{"token": "[redacted]"}],
        "account_id": "acct_123",
    }


def test_health_status_redacts_python_and_json_style_secret_assignments():
    status = HealthStatus(
        name="provider",
        status=HealthState.UNHEALTHY,
        message=(
            "input_value={'api_key': 'sk_live_123', "
            '"password": "secret-password", token=token-123}'
        ),
    )

    assert "sk_live_123" not in status.message
    assert "secret-password" not in status.message
    assert "token-123" not in status.message
    assert "'api_key': '[redacted]'" in status.message
    assert '"password": "[redacted]"' in status.message


def test_public_exports_include_settings_flags_and_health_models():
    from infra.config import InfraSettings as ExportedInfraSettings
    from infra.config import PluginSettings as ExportedPluginSettings
    from infra.core import FeatureFlag as ExportedFeatureFlag
    from infra.core import HealthRegistry as ExportedHealthRegistry
    from infra.core import HealthState as ExportedHealthState
    from infra.core import HealthStatus as ExportedHealthStatus
    from infra.core import resolve_feature_flag as exported_resolve_feature_flag

    assert ExportedInfraSettings is InfraSettings
    assert ExportedPluginSettings is PluginSettings
    assert ExportedFeatureFlag is FeatureFlag
    assert ExportedHealthRegistry is HealthRegistry
    assert ExportedHealthState is HealthState
    assert ExportedHealthStatus is HealthStatus
    assert exported_resolve_feature_flag is resolve_feature_flag
