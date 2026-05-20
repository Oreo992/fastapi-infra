import pytest
from pydantic import BaseModel, ValidationError

from infra.config import validate_infra_settings
from infra.config.models import InfraSettings, PluginSettings
from infra.plugins.ai.plugin import AIPluginConfig
from infra.plugins.auth.plugin import AuthPluginConfig
from infra.plugins.builtin import get_builtin_plugins
from infra.plugins.cache.plugin import CachePluginConfig
from infra.plugins.database.plugin import DatabasePluginConfig
from infra.plugins.http.plugin import HTTPPluginConfig
from infra.plugins.notifications.noop import (
    NotificationsConfig,
    SMTPNotificationConfig,
    WebhookNotificationConfig,
)
from infra.plugins.observability import ObservabilityPluginConfig
from infra.plugins.payment.plugin import PaymentPluginConfig
from infra.plugins.payment.stripe import StripeProviderConfig
from infra.plugins.ratelimit import RateLimitPluginConfig, RedisRateLimitConfig
from infra.plugins.speech.plugin import SpeechPluginConfig
from infra.plugins.speech.providers.openai import OpenAISpeechProviderConfig
from infra.plugins.storage.local import StorageConfig
from infra.plugins.storage.s3 import S3StorageConfig
from infra.plugins.tasks.plugin import RedisTaskQueueConfig, TasksPlugin, TasksPluginConfig
from infra.plugins.webhooks import WebhooksPluginConfig


class RequiredDependencyPlugin:
    metadata = type(
        "Metadata",
        (),
        {
            "name": "dependent",
            "default_enabled": False,
            "dependencies": ["database"],
            "optional_dependencies": [],
        },
    )()
    config_model = None


class MissingRequiredDependencyPlugin:
    metadata = type(
        "Metadata",
        (),
        {
            "name": "dependent",
            "default_enabled": False,
            "dependencies": ["missing"],
            "optional_dependencies": [],
        },
    )()
    config_model = None


class MissingOptionalDependencyPlugin:
    metadata = type(
        "Metadata",
        (),
        {
            "name": "optional",
            "default_enabled": False,
            "dependencies": [],
            "optional_dependencies": ["package_that_does_not_exist_fastapi_infra"],
        },
    )()
    config_model = None


class ManifestConsumerConfig(BaseModel):
    mode: str = "local"
    object_store_service: str = "object_store"


class ManifestConsumerPlugin:
    metadata = type(
        "Metadata",
        (),
        {
            "name": "manifest_consumer",
            "default_enabled": False,
            "dependencies": [],
            "optional_dependencies": [],
            "provides": [],
            "service_name_config": None,
        },
    )()
    config_model = ManifestConsumerConfig
    manifest_hints = {
        "service_references": {
            "object_store_service": {
                "default_service": "object_store",
                "required_when": "mode == 'remote'",
                "required_when_config": {"mode": "remote"},
                "description": "External object store service.",
            }
        }
    }


class ManifestObjectStorePlugin:
    metadata = type(
        "Metadata",
        (),
        {
            "name": "manifest_object_store",
            "default_enabled": False,
            "dependencies": [],
            "optional_dependencies": [],
            "provides": ["object_store"],
            "service_name_config": None,
        },
    )()
    config_model = None


class InvalidManifestReferencePlugin:
    metadata = type(
        "Metadata",
        (),
        {
            "name": "invalid_manifest_reference",
            "default_enabled": False,
            "dependencies": [],
            "optional_dependencies": [],
            "provides": [],
            "service_name_config": None,
        },
    )()
    config_model = ManifestConsumerConfig
    manifest_hints = {
        "service_references": {
            "object_store_service": {
                "default_service": "object_store",
                "unknown": True,
            }
        }
    }


@pytest.mark.parametrize(
    "config_model",
    [
        AIPluginConfig,
        CachePluginConfig,
        DatabasePluginConfig,
        HTTPPluginConfig,
        NotificationsConfig,
        ObservabilityPluginConfig,
        PaymentPluginConfig,
        RateLimitPluginConfig,
        SpeechPluginConfig,
        StorageConfig,
        TasksPluginConfig,
        WebhooksPluginConfig,
    ],
)
def test_plugin_configs_reject_unknown_fields(config_model: type[BaseModel]):
    with pytest.raises(ValidationError):
        config_model.model_validate({"unexpected": True})


def test_infra_settings_reject_unknown_top_level_fields():
    with pytest.raises(ValidationError):
        InfraSettings.model_validate({"infra": {"plugins": {}}, "unexpected": True})


def test_infra_settings_reject_unknown_namespace_fields():
    with pytest.raises(ValidationError):
        InfraSettings.model_validate({"infra": {"pluginz": {}}})


def test_plugin_settings_reject_unknown_fields():
    with pytest.raises(ValidationError):
        PluginSettings.model_validate({"enabled": True, "unexpected": True})


def test_ai_provider_config_rejects_unknown_nested_fields():
    with pytest.raises(ValidationError):
        AIPluginConfig.model_validate(
            {"providers": {"openai": {"api_key": "sk-test", "api_kee": "typo"}}}
        )


def test_database_manager_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        DatabasePluginConfig.model_validate({"config": {"mysql_hots": "typo"}})


def test_cache_database_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        CachePluginConfig.model_validate({"database_config": {"redis_urll": "typo"}})


def test_http_plugin_config_validates_base_url_and_timeout():
    config = HTTPPluginConfig.model_validate(
        {"base_url": "https://api.example.test/", "timeout": 1.5}
    )

    assert config.base_url == "https://api.example.test"
    assert HTTPPluginConfig.model_validate({"base_url": "mock://local"}).base_url == "mock://local"
    with pytest.raises(ValidationError, match="base_url"):
        HTTPPluginConfig.model_validate({"base_url": "api.example.test"})
    with pytest.raises(ValidationError, match="timeout"):
        HTTPPluginConfig.model_validate({"timeout": 0})
    with pytest.raises(ValidationError, match="aiohttp provider"):
        HTTPPluginConfig.model_validate({"default_provider": "aiohttp", "base_url": "mock://local"})


def test_auth_plugin_config_rejects_nonpositive_token_ttl():
    with pytest.raises(ValidationError, match="access_token_ttl_seconds"):
        AuthPluginConfig.model_validate({"access_token_ttl_seconds": 0})


def test_smtp_provider_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        SMTPNotificationConfig.model_validate(
            {
                "host": "smtp.example.com",
                "sender": "noreply@example.com",
                "sendr": "typo",
            }
        )


def test_smtp_provider_config_rejects_invalid_numeric_fields():
    with pytest.raises(ValidationError, match="port"):
        SMTPNotificationConfig.model_validate(
            {"host": "smtp.example.com", "sender": "noreply@example.com", "port": 0}
        )
    with pytest.raises(ValidationError, match="timeout"):
        SMTPNotificationConfig.model_validate(
            {"host": "smtp.example.com", "sender": "noreply@example.com", "timeout": 0}
        )
    with pytest.raises(ValidationError, match="port"):
        SMTPNotificationConfig.model_validate(
            {"host": "smtp.example.com", "sender": "noreply@example.com", "port": True}
        )
    with pytest.raises(ValidationError, match="max_attempts"):
        SMTPNotificationConfig.model_validate(
            {
                "host": "smtp.example.com",
                "sender": "noreply@example.com",
                "max_attempts": True,
            }
        )


def test_smtp_provider_config_rejects_blank_strings():
    with pytest.raises(ValidationError, match="host"):
        SMTPNotificationConfig.model_validate({"host": "   ", "sender": "noreply@example.com"})
    with pytest.raises(ValidationError, match="sender"):
        SMTPNotificationConfig.model_validate({"host": "smtp.example.com", "sender": "   "})


def test_webhook_notification_config_validates_urls_and_numeric_fields():
    config = WebhookNotificationConfig(
        url="https://hooks.example.test/notify",
        health_url="https://hooks.example.test/health",
        timeout=1,
    )

    assert config.url == "https://hooks.example.test/notify"
    with pytest.raises(ValidationError, match="url"):
        WebhookNotificationConfig.model_validate({"url": "hooks.example.test/notify"})
    with pytest.raises(ValidationError, match="timeout"):
        WebhookNotificationConfig.model_validate(
            {"url": "https://hooks.example.test/notify", "timeout": True}
        )


def test_s3_provider_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        S3StorageConfig.model_validate(
            {
                "bucket": "bucket",
                "access_key_id": "access",
                "secret_access_key": "secret",
                "buckt": "typo",
            }
        )


def test_s3_provider_config_rejects_bool_numeric_fields():
    base_config = {
        "bucket": "bucket",
        "access_key_id": "access",
        "secret_access_key": "secret",
    }

    with pytest.raises(ValidationError, match="timeout"):
        S3StorageConfig.model_validate({**base_config, "timeout": True})
    with pytest.raises(ValidationError, match="max_attempts"):
        S3StorageConfig.model_validate({**base_config, "max_attempts": True})
    with pytest.raises(ValidationError, match="retry_base_delay"):
        S3StorageConfig.model_validate({**base_config, "retry_base_delay": True})


def test_stripe_provider_config_rejects_unknown_fields():
    with pytest.raises(ValidationError, match="api_kee"):
        StripeProviderConfig.model_validate({"api_key": "sk_test", "api_kee": "typo"})


def test_payment_config_rejects_unknown_store_service():
    with pytest.raises(ValidationError):
        PaymentPluginConfig.model_validate({"store_service": "unknown"})


def test_tasks_config_rejects_invalid_runtime_fields():
    with pytest.raises(ValidationError, match="service"):
        TasksPluginConfig.model_validate({"service": ""})
    with pytest.raises(ValidationError, match="pending_min_idle_ms"):
        RedisTaskQueueConfig.model_validate({"pending_min_idle_ms": -1})
    with pytest.raises(ValidationError, match="redis"):
        TasksPluginConfig.model_validate({"redis": "redis://localhost:6379/0"})
    with pytest.raises(ValidationError, match="adapter"):
        TasksPluginConfig.model_validate({"adapter": "redis"})
    with pytest.raises(ValidationError, match="pending_min_idle_ms"):
        config = TasksPluginConfig.model_validate(
            {
                "default_provider": "redis",
                "providers": {"redis": {"pending_min_idle_ms": -1}},
            }
        )
        TasksPlugin().validate_config(config)


def test_ratelimit_config_rejects_invalid_runtime_fields():
    with pytest.raises(ValidationError, match="service"):
        RateLimitPluginConfig.model_validate({"service": ""})
    with pytest.raises(ValidationError, match="key_prefix"):
        RedisRateLimitConfig.model_validate({"key_prefix": ""})
    with pytest.raises(ValidationError, match="backend"):
        RateLimitPluginConfig.model_validate({"backend": "redis"})


def test_openai_speech_provider_config_rejects_unknown_fields():
    with pytest.raises(ValidationError, match="api_kee"):
        OpenAISpeechProviderConfig.model_validate({"api_key": "sk-test", "api_kee": "typo"})


def test_validate_infra_settings_reports_unknown_plugins_and_schema_errors():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "providers": {"openai": {"api_kee": "typo"}},
                    },
                },
                "not_real": {"enabled": True},
            }
        }
    )

    issues = validate_infra_settings(settings, get_builtin_plugins())

    assert [(issue.plugin, issue.code) for issue in issues] == [
        ("not_real", "unknown_plugin"),
        ("ai", "invalid_config"),
    ]
    assert "api_kee" in issues[1].message


def test_validate_infra_settings_reports_unknown_ai_provider():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {"default_provider": "unknown"},
                }
            }
        }
    )

    issues = validate_infra_settings(settings, get_builtin_plugins())

    assert [(issue.plugin, issue.code) for issue in issues] == [("ai", "invalid_config")]
    assert "unknown ai provider: unknown" in issues[0].message


def test_validate_infra_settings_reports_inactive_required_dependency():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {"enabled": False},
                "dependent": {"enabled": True},
            }
        }
    )

    issues = validate_infra_settings(
        settings,
        [DatabasePluginConfigPlugin(), RequiredDependencyPlugin()],
    )

    assert [(issue.plugin, issue.code) for issue in issues] == [
        ("dependent", "inactive_dependency")
    ]
    assert issues[0].details == {"inactive_dependencies": ["database"]}


def test_validate_infra_settings_reports_unknown_required_dependency():
    settings = InfraSettings(infra={"plugins": {"dependent": {"enabled": True}}})

    issues = validate_infra_settings(settings, [MissingRequiredDependencyPlugin()])

    assert [(issue.plugin, issue.code) for issue in issues] == [("dependent", "unknown_dependency")]
    assert issues[0].details == {"missing_dependencies": ["missing"]}


def test_validate_infra_settings_reports_missing_optional_dependency():
    settings = InfraSettings(infra={"plugins": {"optional": {"enabled": True}}})

    issues = validate_infra_settings(settings, [MissingOptionalDependencyPlugin()])

    assert [(issue.plugin, issue.code) for issue in issues] == [
        ("optional", "missing_optional_dependency")
    ]
    assert issues[0].details == {
        "missing_optional_dependencies": ["package_that_does_not_exist_fastapi_infra"]
    }


class DatabasePluginConfigPlugin:
    metadata = type(
        "Metadata",
        (),
        {
            "name": "database",
            "default_enabled": False,
            "dependencies": [],
            "optional_dependencies": [],
        },
    )()
    config_model = None


def test_validate_infra_settings_skips_disabled_invalid_plugin_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": False,
                    "config": {
                        "providers": {"openai": {"api_kee": "typo"}},
                    },
                }
            }
        }
    )

    assert validate_infra_settings(settings, get_builtin_plugins()) == []


def test_validate_infra_settings_checks_nested_builtin_provider_configs():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "providers": {"stripe": {"api_key": "sk-test", "api_kee": "typo"}},
                    },
                },
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "providers": {"openai": {"api_key": "sk-test", "api_kee": "typo"}},
                    },
                },
                "storage": {
                    "enabled": True,
                    "config": {
                        "default_provider": "s3",
                        "providers": {
                            "s3": {
                                "bucket": "bucket",
                                "access_key_id": "access",
                                "secret_access_key": "secret",
                                "buckt": "typo",
                            }
                        },
                    },
                },
                "notifications": {
                    "enabled": True,
                    "config": {
                        "default_provider": "smtp",
                        "providers": {
                            "smtp": {
                                "host": "smtp.example.com",
                                "sender": "noreply@example.com",
                                "sendr": "typo",
                            }
                        },
                    },
                },
            }
        }
    )

    issues = validate_infra_settings(settings, get_builtin_plugins())

    assert [(issue.plugin, issue.code) for issue in issues] == [
        ("notifications", "invalid_config"),
        ("payment", "invalid_config"),
        ("speech", "invalid_config"),
        ("storage", "invalid_config"),
    ]
    messages = {issue.plugin: issue.message for issue in issues}
    assert "sendr" in messages["notifications"]
    assert "api_kee" in messages["payment"]
    assert "api_kee" in messages["speech"]
    assert "buckt" in messages["storage"]


def test_validate_infra_settings_reports_missing_payment_store_service_reference():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {"store_service": "database"},
                }
            }
        }
    )

    issues = validate_infra_settings(settings, get_builtin_plugins())

    assert [(issue.plugin, issue.code) for issue in issues] == [
        ("payment", "missing_service_reference")
    ]
    assert issues[0].details["field"] == "store_service"
    assert issues[0].details["service"] == "database"


def test_validate_infra_settings_accepts_payment_store_when_database_is_enabled():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {"enabled": True},
                "payment": {
                    "enabled": True,
                    "config": {"store_service": "database"},
                },
            }
        }
    )

    issues = validate_infra_settings(settings, get_builtin_plugins())

    assert [
        (issue.plugin, issue.code) for issue in issues if issue.code == "missing_service_reference"
    ] == []


def test_validate_infra_settings_reports_missing_redis_service_references():
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {
                        "default_provider": "redis",
                        "providers": {"redis": {"database_service": "database"}},
                    },
                },
                "ratelimit": {
                    "enabled": True,
                    "config": {
                        "default_provider": "redis",
                        "providers": {"redis": {"database_service": "database"}},
                    },
                },
            }
        }
    )

    issues = validate_infra_settings(settings, get_builtin_plugins())

    assert sorted((issue.plugin, issue.code) for issue in issues) == [
        ("ratelimit", "missing_service_reference"),
        ("tasks", "missing_service_reference"),
    ]
    assert {issue.details["field"] for issue in issues} == {
        "providers.redis.database_service",
    }


def test_validate_infra_settings_accepts_redis_service_references_with_database_enabled():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {"enabled": True},
                "tasks": {
                    "enabled": True,
                    "config": {
                        "default_provider": "redis",
                        "providers": {"redis": {"database_service": "database"}},
                    },
                },
                "ratelimit": {
                    "enabled": True,
                    "config": {
                        "default_provider": "redis",
                        "providers": {"redis": {"database_service": "database"}},
                    },
                },
            }
        }
    )

    issues = validate_infra_settings(settings, get_builtin_plugins())

    assert [
        (issue.plugin, issue.code) for issue in issues if issue.code == "missing_service_reference"
    ] == []


def test_validate_infra_settings_uses_manifest_service_references_for_external_plugins():
    settings = InfraSettings(
        infra={
            "plugins": {
                "manifest_consumer": {
                    "enabled": True,
                    "config": {"mode": "remote"},
                }
            }
        }
    )

    issues = validate_infra_settings(settings, [ManifestConsumerPlugin()])

    assert [(issue.plugin, issue.code) for issue in issues] == [
        ("manifest_consumer", "missing_service_reference")
    ]
    assert issues[0].details["field"] == "object_store_service"
    assert issues[0].details["service"] == "object_store"


def test_validate_infra_settings_accepts_manifest_service_reference_when_provider_is_active():
    settings = InfraSettings(
        infra={
            "plugins": {
                "manifest_consumer": {
                    "enabled": True,
                    "config": {"mode": "remote"},
                },
                "manifest_object_store": {"enabled": True},
            }
        }
    )

    issues = validate_infra_settings(
        settings,
        [ManifestConsumerPlugin(), ManifestObjectStorePlugin()],
    )

    assert [
        (issue.plugin, issue.code) for issue in issues if issue.code == "missing_service_reference"
    ] == []


def test_validate_infra_settings_reports_invalid_manifest_service_reference_shape():
    settings = InfraSettings(infra={"plugins": {"invalid_manifest_reference": {"enabled": True}}})

    issues = validate_infra_settings(settings, [InvalidManifestReferencePlugin()])

    assert [(issue.plugin, issue.code) for issue in issues] == [
        ("invalid_manifest_reference", "invalid_manifest")
    ]
    assert issues[0].details["errors"]
