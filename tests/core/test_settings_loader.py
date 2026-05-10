import json

import pytest

from infra.config import InfraSettings, load_infra_settings


def test_load_infra_settings_returns_defaults_for_missing_path(tmp_path):
    settings = load_infra_settings(tmp_path / "missing.json")

    assert isinstance(settings, InfraSettings)
    assert settings.infra.plugins == {}


def test_load_infra_settings_reads_json_file(tmp_path):
    config_path = tmp_path / "infra.json"
    config_path.write_text(
        json.dumps({"infra": {"plugins": {"payment": {"enabled": False}}}}),
        encoding="utf-8",
    )

    settings = load_infra_settings(config_path)

    assert settings.get_plugin("payment").enabled is False


def test_load_infra_settings_reads_toml_file(tmp_path):
    config_path = tmp_path / "infra.toml"
    config_path.write_text(
        """
[infra.plugins.payment]
enabled = true
""",
        encoding="utf-8",
    )

    settings = load_infra_settings(config_path)

    assert settings.get_plugin("payment").enabled is True


def test_load_infra_settings_environment_overrides_file_values(tmp_path, monkeypatch):
    config_path = tmp_path / "infra.json"
    config_path.write_text(
        json.dumps({"infra": {"plugins": {"payment": {"enabled": True}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("INFRA__INFRA__PLUGINS__PAYMENT__ENABLED", "false")

    settings = load_infra_settings(config_path)

    assert settings.get_plugin("payment").enabled is False


def test_load_infra_settings_reads_nested_plugin_config_from_environment(monkeypatch):
    monkeypatch.setenv("INFRA__INFRA__PLUGINS__AUTH__CONFIG__JWT_SECRET", "secret")
    monkeypatch.setenv("INFRA__INFRA__PLUGINS__AUTH__CONFIG__ISSUERS", '["api", "admin"]')
    monkeypatch.setenv("INFRA__INFRA__PLUGINS__AUTH__CONFIG__RETRIES", "3")
    monkeypatch.setenv("INFRA__INFRA__PLUGINS__AUTH__CONFIG__STRICT", "true")
    monkeypatch.setenv("INFRA__INFRA__PLUGINS__AUTH__CONFIG__LEGACY", "null")

    settings = load_infra_settings()

    assert settings.get_plugin("auth").config == {
        "jwt_secret": "secret",
        "issuers": ["api", "admin"],
        "retries": 3,
        "strict": True,
        "legacy": None,
    }


def test_load_infra_settings_rejects_unsupported_extension(tmp_path):
    config_path = tmp_path / "infra.yaml"
    config_path.write_text("infra: {}", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported config extension"):
        load_infra_settings(config_path)


def test_load_infra_settings_is_not_exported_from_top_level_infra():
    import infra

    assert not hasattr(infra, "load_infra_settings")
