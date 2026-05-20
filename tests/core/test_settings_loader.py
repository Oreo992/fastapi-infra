import json

import pytest

from infra.config import InfraSettings, load_env_file, load_infra_settings


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


def test_load_infra_settings_resolves_file_env_references(tmp_path, monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk-live")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec-live")
    config_path = tmp_path / "infra.json"
    config_path.write_text(
        json.dumps(
            {
                "infra": {
                    "plugins": {
                        "payment": {
                            "enabled": True,
                            "config": {
                                "default_provider": "stripe",
                                "providers": {
                                    "stripe": {
                                        "api_key": {"$env": "STRIPE_API_KEY"},
                                        "webhook_secret": {"$env": "STRIPE_WEBHOOK_SECRET"},
                                    }
                                },
                            },
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    settings = load_infra_settings(config_path)

    stripe_config = settings.get_plugin("payment").config["providers"]["stripe"]
    assert stripe_config == {
        "api_key": "sk-live",
        "webhook_secret": "whsec-live",
    }


def test_load_infra_settings_resolves_environment_override_env_references(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv(
        "INFRA__INFRA__PLUGINS__AI__CONFIG__PROVIDERS__OPENAI__API_KEY",
        '{"$env":"OPENAI_API_KEY"}',
    )

    settings = load_infra_settings()

    assert settings.get_plugin("ai").config["providers"]["openai"]["api_key"] == "sk-openai"


def test_load_infra_settings_rejects_missing_env_reference(tmp_path):
    config_path = tmp_path / "infra.json"
    config_path.write_text(
        json.dumps(
            {
                "infra": {
                    "plugins": {
                        "payment": {
                            "config": {
                                "providers": {"stripe": {"api_key": {"$env": "MISSING_SECRET"}}}
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="MISSING_SECRET"):
        load_infra_settings(config_path)


def test_load_infra_settings_can_use_placeholders_for_missing_env_references(tmp_path):
    config_path = tmp_path / "infra.json"
    config_path.write_text(
        json.dumps(
            {
                "infra": {
                    "plugins": {
                        "database": {
                            "config": {
                                "config": {
                                    "mysql_port": {"$env": "MYSQL_PORT"},
                                    "redis_url": {"$env": "REDIS_URL"},
                                }
                            }
                        },
                        "payment": {
                            "config": {
                                "providers": {"stripe": {"api_key": {"$env": "STRIPE_API_KEY"}}}
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    settings = load_infra_settings(config_path, missing_env="placeholder")

    database_config = settings.get_plugin("database").config["config"]
    stripe_config = settings.get_plugin("payment").config["providers"]["stripe"]
    assert database_config["mysql_port"] == "1"
    assert database_config["redis_url"] == "redis://localhost:6379/0"
    assert stripe_config["api_key"] == "placeholder-api-key"


def test_load_infra_settings_rejects_malformed_env_reference(tmp_path):
    config_path = tmp_path / "infra.json"
    config_path.write_text(
        json.dumps({"infra": {"plugins": {"auth": {"config": {"jwt_secret": {"$env": ""}}}}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-empty string"):
        load_infra_settings(config_path)


def test_load_infra_settings_rejects_unsupported_extension(tmp_path):
    config_path = tmp_path / "infra.yaml"
    config_path.write_text("infra: {}", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported config extension"):
        load_infra_settings(config_path)


def test_load_env_file_merges_dotenv_values(tmp_path):
    env_file = tmp_path / "local.env"
    env_file.write_text(
        """
# local credentials
export STRIPE_API_KEY=sk-file
STRIPE_WEBHOOK_SECRET='whsec file'
OPENAI_API_KEY="sk\\nopenai"
ANTHROPIC_API_KEY="sk\\qpreserved" # inline quoted comment
SMTP_LIVE_HOST=smtp.example.com # inline comment
""",
        encoding="utf-8",
    )

    env = load_env_file(env_file, base_environ={"STRIPE_API_KEY": "sk-base"})

    assert env["STRIPE_API_KEY"] == "sk-file"
    assert env["STRIPE_WEBHOOK_SECRET"] == "whsec file"
    assert env["OPENAI_API_KEY"] == "sk\nopenai"
    assert env["ANTHROPIC_API_KEY"] == "sk\\qpreserved"
    assert env["SMTP_LIVE_HOST"] == "smtp.example.com"


def test_load_env_file_rejects_invalid_lines(tmp_path):
    env_file = tmp_path / "local.env"
    env_file.write_text("not-an-assignment\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1: .*KEY=VALUE"):
        load_env_file(env_file)


def test_load_env_file_rejects_non_ascii_keys(tmp_path):
    env_file = tmp_path / "local.env"
    env_file.write_text("密钥=value\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1: invalid key"):
        load_env_file(env_file)


def test_load_infra_settings_is_not_exported_from_top_level_infra():
    import infra

    assert not hasattr(infra, "load_infra_settings")
