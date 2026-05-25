from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from infra.config.models import InfraSettings
from infra.plugins.auth import hash_api_key
from infra.plugins.contract import PluginMetadata
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginProviderPolicy,
    PluginReleaseDependency,
    PluginReleaseIssue,
    provider_certification,
    provider_policy,
    release_dependency,
)
from infra.provider_certification import (
    DEFAULT_LIVE_PROVIDER_TEST_PATH,
    DEFAULT_PROVIDER_CHECKS,
    ProviderCheck,
)
from infra.release_check import (
    ReleaseCheckIssue,
    build_release_check_report,
    expected_provider_check_names,
    format_release_check_text,
)

_PROVIDER_CHECKS = {check.name: check for check in DEFAULT_PROVIDER_CHECKS}


def test_expected_provider_check_names_expands_configured_provider_dependencies():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-stripe",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                }
            }
        }
    )

    assert expected_provider_check_names(settings) == ("mysql", "stripe")


def test_expected_provider_check_names_returns_empty_when_no_certified_provider_is_configured():
    settings = InfraSettings(infra={"plugins": {}})

    assert expected_provider_check_names(settings) == ()


class ExternalReleaseCheckPlugin:
    metadata = PluginMetadata(name="external", version="1.0.0")
    config_model = None

    def release_check(self, settings: InfraSettings, config: object) -> list[ReleaseCheckIssue]:
        return [
            ReleaseCheckIssue(
                plugin="external",
                code="not_ready",
                message="external plugin is not production ready",
            )
        ]


class InvalidReleaseCheckPlugin:
    metadata = PluginMetadata(name="invalid_external", version="1.0.0")
    config_model = None

    def release_check(self, settings: InfraSettings, config: object) -> list[object]:
        return [object()]


class MappingReleaseCheckPlugin:
    metadata = PluginMetadata(name="mapping_external", version="1.0.0")
    config_model = None

    def release_check(self, settings: InfraSettings, config: object) -> list[PluginReleaseIssue]:
        return [
            {
                "code": "manual_review",
                "message": "external plugin needs a production owner review",
                "severity": "warning",
            }
        ]


class InvalidMappingReleaseCheckPlugin:
    metadata = PluginMetadata(name="invalid_mapping_external", version="1.0.0")
    config_model = None

    def release_check(self, settings: InfraSettings, config: object) -> list[dict[str, str]]:
        return [{"message": "missing code"}]


class ReleaseDependencyPlugin:
    metadata = PluginMetadata(name="dependency_external", version="1.0.0")
    config_model = None

    def release_dependencies(
        self, settings: InfraSettings, config: object
    ) -> list[PluginReleaseDependency]:
        return [
            release_dependency(
                "target",
                "target_flag_required",
                "dependency_external requires target.config.flags to include enabled",
                config_path="flags",
                contains="enabled",
            )
        ]


class ReleaseDependencyTargetPlugin:
    metadata = PluginMetadata(name="target", version="1.0.0")
    config_model = None


class InvalidReleaseDependencyPlugin:
    metadata = PluginMetadata(name="invalid_dependency_external", version="1.0.0")
    config_model = None

    def release_dependencies(self, settings: InfraSettings, config: object) -> list[object]:
        return [object()]


class SecretMappingReleaseCheckPlugin:
    metadata = PluginMetadata(name="secret_mapping_external", version="1.0.0")
    config_model = None

    def release_check(self, settings: InfraSettings, config: object) -> list[PluginReleaseIssue]:
        return [
            {
                "code": "secret_message",
                "message": "provider password=real-secret-value",
            }
        ]


class ProviderPolicyPlugin:
    metadata = PluginMetadata(name="provider_policy_external", version="1.0.0")
    config_model = None

    def provider_release_policies(
        self, settings: InfraSettings, config: object
    ) -> list[PluginProviderPolicy]:
        return [
            provider_policy(
                "search",
                {"acme"},
                local_providers=set(),
                health_probe=False,
            )
        ]


class InvalidProviderPolicyPlugin:
    metadata = PluginMetadata(name="invalid_provider_policy_external", version="1.0.0")
    config_model = None

    def provider_release_policies(self, settings: InfraSettings, config: object) -> list[object]:
        return [object()]


class ProviderCertificationPlugin:
    metadata = PluginMetadata(name="provider_certification_external", version="1.0.0")
    config_model = None

    def provider_certifications(
        self, settings: InfraSettings, config: object
    ) -> list[PluginProviderCertification]:
        return [provider_certification("search", "acme")]


class InvalidProviderCertificationPlugin:
    metadata = PluginMetadata(name="invalid_provider_certification_external", version="1.0.0")
    config_model = None

    def provider_certifications(self, settings: InfraSettings, config: object) -> list[object]:
        return [object()]


class FakeProviderEntryPoint:
    name = "acme"


def _passed_provider(name: str, *, missing_env: list[str] | None = None) -> dict:
    check = _PROVIDER_CHECKS.get(name)
    return {
        "name": name,
        "outcome": "passed",
        "tests": list(check.tests) if check is not None else [f"test_live_{name}"],
        "details": [
            f"tests/integration/test_live_providers.py::{test_name}"
            for test_name in (check.tests if check is not None else (f"test_live_{name}",))
        ],
        "requirements": {
            "required_env": list(check.required_env) if check is not None else [],
            "optional_env": list(check.optional_env) if check is not None else [],
            "required_packages": list(check.required_packages) if check is not None else [],
            "missing_required_env": missing_env or [],
            "missing_required_packages": [],
        },
    }


def _fresh_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stale_timestamp() -> str:
    return (
        (datetime.now(UTC) - timedelta(hours=25))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _passed_certification_report(providers: list[str]) -> dict:
    return {
        "certified": True,
        "generated_at": _fresh_timestamp(),
        "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
        "selected_providers": providers,
        "summary": {
            "total": len(providers),
            "passed": len(providers),
            "failed": 0,
            "skipped": 0,
            "missing": 0,
        },
        "providers": [_passed_provider(provider) for provider in providers],
    }


def _passed_custom_provider(
    name: str,
    *,
    test_path: str,
    tests: tuple[str, ...],
    required_env: tuple[str, ...] = (),
    required_packages: tuple[str, ...] = (),
) -> dict:
    return {
        "name": name,
        "outcome": "passed",
        "test_path": test_path,
        "tests": list(tests),
        "details": [f"{test_path}::{test_name}" for test_name in tests],
        "requirements": {
            "required_env": list(required_env),
            "optional_env": [],
            "required_packages": list(required_packages),
            "missing_required_env": [],
            "missing_required_packages": [],
        },
    }


def _stripe_webhooks_plugin() -> dict:
    return {
        "enabled": True,
        "config": {
            "durable_store": True,
            "providers": {"stripe": {"webhook_secret": "whsec_test"}},
            "required_providers": ["stripe"],
        },
    }


def test_release_check_allows_empty_optional_infra():
    report = build_release_check_report(InfraSettings())

    assert report["ready"] is True
    assert report["summary"] == {"errors": 0, "warnings": 0}
    assert report["issues"] == []


def test_release_check_runs_enabled_plugin_release_check_hooks():
    settings = InfraSettings(infra={"plugins": {"external": {"enabled": True}}})

    report = build_release_check_report(
        settings,
        plugins=[ExternalReleaseCheckPlugin()],
        require_provider_certification=False,
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "external",
            "code": "not_ready",
            "message": "external plugin is not production ready",
        }
    ]


def test_release_check_reports_invalid_plugin_release_check_hooks():
    settings = InfraSettings(infra={"plugins": {"invalid_external": {"enabled": True}}})

    report = build_release_check_report(
        settings,
        plugins=[InvalidReleaseCheckPlugin()],
        require_provider_certification=False,
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "invalid_external",
            "code": "release_check_invalid",
            "message": "plugin release_check must return ReleaseCheckIssue items or issue mappings",
        }
    ]


def test_release_check_accepts_plugin_release_check_issue_mappings():
    settings = InfraSettings(infra={"plugins": {"mapping_external": {"enabled": True}}})

    report = build_release_check_report(
        settings,
        plugins=[MappingReleaseCheckPlugin()],
        require_provider_certification=False,
    )

    assert report["ready"] is True
    assert report["summary"] == {"errors": 0, "warnings": 1}
    assert report["issues"] == [
        {
            "severity": "warning",
            "plugin": "mapping_external",
            "code": "manual_review",
            "message": "external plugin needs a production owner review",
        }
    ]


def test_release_check_rejects_invalid_plugin_release_check_issue_mappings():
    settings = InfraSettings(infra={"plugins": {"invalid_mapping_external": {"enabled": True}}})

    report = build_release_check_report(
        settings,
        plugins=[InvalidMappingReleaseCheckPlugin()],
        require_provider_certification=False,
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "invalid_mapping_external",
            "code": "release_check_invalid",
            "message": "plugin release_check must return ReleaseCheckIssue items or issue mappings",
        }
    ]


def test_release_check_runs_enabled_plugin_release_dependencies():
    settings = InfraSettings(
        infra={
            "plugins": {
                "dependency_external": {"enabled": True},
                "target": {
                    "enabled": True,
                    "config": {"flags": []},
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        plugins=[ReleaseDependencyPlugin(), ReleaseDependencyTargetPlugin()],
        require_provider_certification=False,
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "dependency_external",
            "code": "target_flag_required",
            "message": "dependency_external requires target.config.flags to include enabled",
        }
    ]


def test_release_check_accepts_satisfied_plugin_release_dependencies():
    settings = InfraSettings(
        infra={
            "plugins": {
                "dependency_external": {"enabled": True},
                "target": {
                    "enabled": True,
                    "config": {"flags": ["enabled"]},
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        plugins=[ReleaseDependencyPlugin(), ReleaseDependencyTargetPlugin()],
        require_provider_certification=False,
    )

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_rejects_invalid_plugin_release_dependencies():
    settings = InfraSettings(infra={"plugins": {"invalid_dependency_external": {"enabled": True}}})

    report = build_release_check_report(
        settings,
        plugins=[InvalidReleaseDependencyPlugin()],
        require_provider_certification=False,
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "invalid_dependency_external",
            "code": "release_dependency_invalid",
            "message": "plugin release_dependencies must return dependency mappings",
        }
    ]


def test_release_check_runs_enabled_plugin_provider_release_policies():
    settings = InfraSettings(infra={"plugins": {"provider_policy_external": {"enabled": True}}})
    acme_check = ProviderCheck(
        name="acme-search",
        provider_kind="search",
        provider_name="acme",
        tests=("test_live_acme_search",),
    )

    report = build_release_check_report(
        settings,
        plugins=[ProviderPolicyPlugin()],
        provider_checks=(*DEFAULT_PROVIDER_CHECKS, acme_check),
        require_provider_certification=False,
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "provider_policy_external",
            "code": "health_probe_required",
            "message": "external provider must enable health_probe in production",
        }
    ]


def test_release_check_reports_invalid_plugin_provider_release_policies():
    settings = InfraSettings(
        infra={"plugins": {"invalid_provider_policy_external": {"enabled": True}}}
    )

    report = build_release_check_report(
        settings,
        plugins=[InvalidProviderPolicyPlugin()],
        require_provider_certification=False,
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "invalid_provider_policy_external",
            "code": "provider_policy_invalid",
            "message": "plugin provider_release_policies must return provider policy mappings",
        }
    ]


def test_release_check_uses_plugin_provider_certification_declarations():
    settings = InfraSettings(
        infra={"plugins": {"provider_certification_external": {"enabled": True}}}
    )
    acme_check = ProviderCheck(
        name="acme-search",
        provider_kind="search",
        provider_name="acme",
        tests=("test_live_acme_search",),
    )

    report = build_release_check_report(
        settings,
        plugins=[ProviderCertificationPlugin()],
        provider_checks=(*DEFAULT_PROVIDER_CHECKS, acme_check),
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_report_required",
            "message": "provider certification report is required for: acme-search",
        }
    ]


def test_release_check_python_api_loads_default_provider_check_catalog(monkeypatch):
    import infra.release_check as release_check

    settings = InfraSettings(
        infra={"plugins": {"provider_certification_external": {"enabled": True}}}
    )
    acme_check = ProviderCheck(
        name="acme-search",
        provider_kind="search",
        provider_name="acme",
        tests=("test_live_acme_search",),
    )

    monkeypatch.setattr(
        release_check,
        "get_provider_checks",
        lambda: (*DEFAULT_PROVIDER_CHECKS, acme_check),
    )

    report = build_release_check_report(
        settings,
        plugins=[ProviderCertificationPlugin()],
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_report_required",
            "message": "provider certification report is required for: acme-search",
        }
    ]


def test_release_check_reports_invalid_plugin_provider_certification_declarations():
    settings = InfraSettings(
        infra={"plugins": {"invalid_provider_certification_external": {"enabled": True}}}
    )

    report = build_release_check_report(
        settings,
        plugins=[InvalidProviderCertificationPlugin()],
        require_provider_certification=False,
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "invalid_provider_certification_external",
            "code": "provider_certification_invalid",
            "message": (
                "plugin provider_certifications must return provider certification mappings"
            ),
        }
    ]


def test_release_check_validates_http_config_when_enabled():
    settings = InfraSettings(
        infra={
            "plugins": {
                "http": {
                    "enabled": True,
                    "config": {"base_url": "ftp://api.example.com"},
                }
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert report["issues"][0]["plugin"] == "http"
    assert report["issues"][0]["code"] == "config_invalid"


def test_release_check_reports_unknown_configured_plugins():
    settings = InfraSettings(infra={"plugins": {"not_real": {"enabled": True}}})

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "not_real",
            "code": "unknown_plugin",
            "message": "unknown configured plugin: not_real",
        }
    ]


def test_release_check_deduplicates_static_schema_errors():
    settings = InfraSettings(
        infra={
            "plugins": {
                "http": {
                    "enabled": True,
                    "config": {"base_url": "api.example.test"},
                }
            }
        }
    )

    report = build_release_check_report(settings)

    assert [(issue["plugin"], issue["code"]) for issue in report["issues"]] == [
        ("http", "config_invalid")
    ]


def test_release_check_deduplicates_nested_provider_schema_errors():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "providers": {"stripe": {"api_key": "sk-test", "api_kee": "typo"}},
                    },
                }
            }
        }
    )

    report = build_release_check_report(settings, require_provider_certification=False)
    codes = [issue["code"] for issue in report["issues"]]

    assert "stripe_config_invalid" in codes
    assert "config_invalid" not in codes


def test_release_check_blocks_mock_and_local_production_providers():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {"enabled": True, "config": {}},
                "ai": {"enabled": True, "config": {"default_provider": "mock"}},
                "payment": {"enabled": True, "config": {"default_provider": "mock"}},
                "storage": {"enabled": True, "config": {"default_provider": "local"}},
                "speech": {"enabled": True, "config": {"default_provider": "mock"}},
                "notifications": {"enabled": True, "config": {"default_provider": "noop"}},
                "tasks": {"enabled": True, "config": {"default_provider": "memory"}},
                "webhooks": {"enabled": True, "config": {}},
                "ratelimit": {"enabled": True, "config": {}},
                "cache": {
                    "enabled": True,
                    "config": {"database_config": {"redis_enabled": False}},
                },
            }
        }
    )

    report = build_release_check_report(settings)
    codes = {(issue["plugin"], issue["code"]) for issue in report["issues"]}
    text = format_release_check_text(settings)

    assert report["ready"] is False
    assert ("auth", "credentials_required") in codes
    assert ("ai", "mock_provider") in codes
    assert ("payment", "mock_provider") in codes
    assert ("storage", "local_provider") in codes
    assert ("speech", "mock_provider") in codes
    assert ("notifications", "noop_provider") in codes
    assert ("tasks", "memory_provider") in codes
    assert ("webhooks", "durable_store_required") in codes
    assert ("webhooks", "providers_required") in codes
    assert ("ratelimit", "memory_provider") in codes
    assert ("cache", "redis_required") in codes
    assert "release-check: blocked" in text


def test_release_check_validates_enabled_http_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "http": {
                    "enabled": True,
                    "config": {"base_url": "api.example.test", "timeout": 0},
                }
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert report["issues"][0]["plugin"] == "http"
    assert report["issues"][0]["code"] == "config_invalid"
    assert "base_url" in report["issues"][0]["message"]
    assert "timeout" in report["issues"][0]["message"]


def test_release_check_blocks_weak_auth_jwt_secret():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {"jwt_secret": "secret"},
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "auth",
        "code": "weak_jwt_secret",
        "message": "production jwt_secret must be at least 32 characters and must not use a placeholder",
    } in report["issues"]


def test_release_check_blocks_weak_auth_jwt_signing_key():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "jwt_key_id": "current",
                        "jwt_signing_keys": {
                            "current": {"secret": "short-secret"},
                        },
                    },
                },
                "webhooks": _stripe_webhooks_plugin(),
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "auth",
        "code": "weak_jwt_signing_key",
        "message": "JWT signing key 'current' must be at least 32 characters and must not use a placeholder",
    } in report["issues"]


def test_release_check_blocks_invalid_auth_api_key_hash():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "hashed_api_keys": {
                            "primary": {
                                "key_hash": "plain-secret",
                                "subject": "service-1",
                            },
                        },
                    },
                },
                "webhooks": _stripe_webhooks_plugin(),
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "auth",
        "code": "api_key_hash_invalid",
        "message": "API key hash 'primary' must use pbkdf2_sha256 with at least 260000 iterations",
    } in report["issues"]


def test_release_check_blocks_under_strength_auth_api_key_hash_iterations():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "hashed_api_keys": {
                            "primary": {
                                "key_hash": "pbkdf2_sha256$1$c2FsdA$ZGlnZXN0",
                                "subject": "service-1",
                            },
                        },
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "auth",
        "code": "api_key_hash_iterations_too_low",
        "message": "API key hash 'primary' must use pbkdf2_sha256 with at least 260000 iterations",
    } in report["issues"]


def test_release_check_accepts_strong_auth_credentials():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "jwt_signing_keys": {
                            "current": {
                                "secret": "production-jwt-secret-at-least-32-chars",
                            },
                        },
                        "hashed_api_keys": {
                            "primary": {
                                "key_hash": hash_api_key("real-api-key", salt=b"fixed-salt"),
                                "subject": "service-1",
                            },
                        },
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is True
    assert report["issues"] == []


HARDENED_EXTERNAL_PROVIDER_PLUGINS = {
    "database": {
        "enabled": True,
        "config": {
            "default_provider": "connections",
            "connect_on_startup": True,
        },
    },
    "auth": {
        "enabled": True,
        "config": {"jwt_secret": "production-jwt-secret-at-least-32-chars"},
    },
    "ai": {
        "enabled": True,
        "config": {
            "default_provider": "openai",
            "health_probe": True,
            "providers": {"openai": {"api_key": "sk-test"}},
        },
    },
    "payment": {
        "enabled": True,
        "config": {
            "default_provider": "stripe",
            "health_probe": True,
            "store_service": "database",
            "providers": {
                "stripe": {
                    "api_key": "sk-test",
                    "webhook_secret": "whsec_test",
                }
            },
        },
    },
    "storage": {
        "enabled": True,
        "config": {
            "default_provider": "s3",
            "health_probe": True,
            "providers": {
                "s3": {
                    "bucket": "bucket",
                    "region": "us-east-1",
                    "access_key_id": "key",
                    "secret_access_key": "secret",
                }
            },
        },
    },
    "speech": {
        "enabled": True,
        "config": {
            "default_provider": "openai",
            "health_probe": True,
            "providers": {"openai": {"api_key": "sk-test"}},
        },
    },
    "notifications": {
        "enabled": True,
        "config": {
            "default_provider": "smtp",
            "health_probe": True,
            "providers": {"smtp": {"host": "smtp.example.com", "sender": "n@example.com"}},
        },
    },
    "tasks": {
        "enabled": True,
        "config": {"default_provider": "redis"},
    },
    "webhooks": {
        "enabled": True,
        "config": {
            "durable_store": True,
            "providers": {"stripe": {"webhook_secret": "whsec_test"}},
            "required_providers": ["stripe"],
        },
    },
    "cache": {
        "enabled": True,
        "config": {
            "default_provider": "redis",
            "database_config": {"redis_enabled": True},
        },
    },
}


def test_release_check_accepts_hardened_external_provider_config():
    settings = InfraSettings(infra={"plugins": HARDENED_EXTERNAL_PROVIDER_PLUGINS})

    report = build_release_check_report(
        settings,
        provider_certification_report=_passed_certification_report(
            [
                "mysql",
                "redis",
                "stripe",
                "s3",
                "openai-ai",
                "openai-speech",
                "smtp",
            ]
        ),
    )

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_requires_provider_certification_report_by_default():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_report_required",
            "message": "provider certification report is required for: openai-ai",
        }
    ]


def test_release_check_can_run_static_only_without_provider_certification_report():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings, require_provider_certification=False)

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_blocks_failed_provider_certification_report():
    settings = InfraSettings()

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": False,
            "selected_providers": ["stripe"],
            "summary": {"total": 1, "passed": 0, "failed": 1},
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_not_passed",
            "message": "provider certification report is not certified",
        }
    ]


def test_release_check_accepts_passed_provider_certification_report():
    settings = InfraSettings()

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["stripe"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("stripe")],
        },
    )

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_validates_certification_report_schema_without_configured_providers():
    settings = InfraSettings()

    report = build_release_check_report(
        settings,
        require_provider_certification=True,
        provider_certification_report={
            "certified": True,
            "selected_providers": ["stripe"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification report is missing generated_at",
        }
    ]


def test_release_check_accepts_provider_certification_report_with_passing_provider_result():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_requires_certification_report_timestamp_for_configured_providers():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification report is missing generated_at",
        }
    ]


def test_release_check_rejects_invalid_certification_report_timestamp():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": "2026-05-12 00:00:00",
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification report has invalid generated_at",
        }
    ]


def test_release_check_rejects_stale_certification_report_for_configured_providers():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _stale_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_stale",
            "message": "provider certification report is older than 24 hours",
        }
    ]


def test_release_check_rejects_future_certification_report_timestamp():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )
    future = (
        (datetime.now(UTC) + timedelta(hours=1))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": future,
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification report generated_at is in the future",
        }
    ]


def test_release_check_rejects_certification_report_from_custom_test_path():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": "tests/custom_live_providers.py",
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": (
                "provider certification report does not cover required test paths: "
                "tests/integration/test_live_providers.py"
            ),
        }
    ]


def test_release_check_rejects_certification_summary_without_all_passed():
    settings = InfraSettings()

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "selected_providers": ["stripe"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 1, "missing": 0},
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification summary does not show all providers passed",
        }
    ]


def test_release_check_requires_provider_result_entries_for_configured_providers():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification report does not include provider results",
        }
    ]


def test_release_check_rejects_certification_summary_that_does_not_match_provider_results():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 2, "passed": 2, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification summary does not match provider results",
        }
    ]


def test_release_check_rejects_invalid_provider_result_entries():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [{"name": "openai-ai", "outcome": "unknown"}],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification report has invalid provider result entries",
        }
    ]


def test_release_check_rejects_duplicate_provider_results():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 2, "passed": 2, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai"), _passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": (
                "provider certification report has duplicate provider results for: openai-ai"
            ),
        }
    ]


def test_release_check_rejects_invalid_selected_provider_entries():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai", 123],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification report has invalid selected providers",
        }
    ]


def test_release_check_rejects_duplicate_selected_providers():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai", "openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification report has duplicate selected providers: openai-ai",
        }
    ]


def test_release_check_rejects_unknown_selected_providers():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai", "custom-provider"],
            "summary": {"total": 2, "passed": 2, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [
                _passed_provider("openai-ai"),
                {
                    "name": "custom-provider",
                    "outcome": "passed",
                    "tests": ["test_live_custom_provider"],
                    "details": [
                        "tests/integration/test_live_providers.py::test_live_custom_provider"
                    ],
                    "requirements": {
                        "required_env": [],
                        "optional_env": [],
                        "required_packages": [],
                        "missing_required_env": [],
                        "missing_required_packages": [],
                    },
                },
            ],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": "provider certification report has unknown selected providers: custom-provider",
        }
    ]


def test_release_check_rejects_selected_providers_that_do_not_match_provider_results():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai", "s3"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai")],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_invalid",
            "message": ("provider certification selected providers do not match provider results"),
        }
    ]


def test_release_check_rejects_provider_result_missing_required_test_evidence():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "connect_on_startup": True,
                        "config": {"redis_enabled": False},
                    },
                },
                "webhooks": _stripe_webhooks_plugin(),
            }
        }
    )
    stripe = _passed_provider("stripe")
    stripe["tests"] = ["test_live_stripe_checkout_session_creation"]

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["mysql", "stripe"],
            "summary": {"total": 2, "passed": 2, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("mysql"), stripe],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_provider_tests_missing",
            "message": (
                "provider certification report is missing required test evidence for: stripe"
            ),
        }
    ]


def test_release_check_rejects_provider_result_missing_test_detail_evidence():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )
    openai = _passed_provider("openai-ai")
    openai["details"] = []

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [openai],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_provider_tests_missing",
            "message": (
                "provider certification report is missing required test evidence for: " "openai-ai"
            ),
        }
    ]


def test_release_check_rejects_provider_result_with_only_similar_test_detail_evidence():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )
    openai = _passed_provider("openai-ai")
    openai["details"] = [
        "tests/integration/test_live_providers.py::test_live_openai_chat_and_embedding_extra"
    ]

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [openai],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_provider_tests_missing",
            "message": (
                "provider certification report is missing required test evidence for: " "openai-ai"
            ),
        }
    ]


def test_release_check_accepts_parametrized_provider_test_detail_evidence():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )
    openai = _passed_provider("openai-ai")
    openai["details"] = [
        "tests/integration/test_live_providers.py::test_live_openai_chat_and_embedding[live]"
    ]

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [openai],
        },
    )

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_rejects_provider_result_detail_from_wrong_test_path():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )
    openai = _passed_provider("openai-ai")
    openai["details"] = ["tests/fake.py::test_live_openai_chat_and_embedding"]

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [openai],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_provider_tests_missing",
            "message": (
                "provider certification report is missing required test evidence for: " "openai-ai"
            ),
        }
    ]


def test_release_check_rejects_provider_result_missing_requirement_metadata():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )
    openai = _passed_provider("openai-ai")
    openai["requirements"]["required_env"] = []

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [openai],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_provider_requirements_incomplete",
            "message": (
                "provider certification report is missing required requirement metadata for: "
                "openai-ai"
            ),
        }
    ]


def test_release_check_rejects_provider_result_with_unmet_requirements():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("openai-ai", missing_env=["OPENAI_API_KEY"])],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_provider_requirements_missing",
            "message": "provider certification report has unmet requirements for: openai-ai",
        }
    ]


def test_release_check_rejects_provider_result_with_invalid_missing_requirement_fields():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )
    openai = _passed_provider("openai-ai")
    openai["requirements"]["missing_required_env"] = None

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "generated_at": _fresh_timestamp(),
            "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [openai],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_provider_requirements_missing",
            "message": "provider certification report has unmet requirements for: openai-ai",
        }
    ]


def test_release_check_requires_certification_report_to_cover_configured_provider():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "connect_on_startup": True,
                        "config": {"redis_enabled": False},
                    },
                },
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
                "webhooks": _stripe_webhooks_plugin(),
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "selected_providers": ["s3"],
            "summary": {"total": 1, "passed": 1, "failed": 0},
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_missing_provider",
            "message": "provider certification report does not cover: mysql, stripe",
        }
    ]


def test_release_check_expands_stripe_certification_dependency_without_database_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
                "webhooks": _stripe_webhooks_plugin(),
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "selected_providers": ["stripe"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
        },
    )

    assert {
        "severity": "error",
        "plugin": "providers",
        "code": "certification_missing_provider",
        "message": "provider certification report does not cover: mysql",
    } in report["issues"]


def test_release_check_requires_certification_for_all_configured_ai_providers():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {
                            "openai": {"api_key": "sk-test"},
                            "anthropic": {"api_key": "sk-ant-test"},
                            "gemini": {"api_key": "gemini-test"},
                        },
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "selected_providers": ["openai-ai"],
            "summary": {"total": 1, "passed": 1, "failed": 0},
        },
    )

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_missing_provider",
            "message": ("provider certification report does not cover: anthropic-ai, gemini-ai"),
        }
    ]


def test_release_check_requires_certification_for_configured_non_default_providers():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "mock",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "mock",
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
                "notifications": {
                    "enabled": True,
                    "config": {
                        "default_provider": "noop",
                        "providers": {
                            "smtp": {"host": "smtp.example.com", "sender": "n@example.com"}
                        },
                    },
                },
                "storage": {
                    "enabled": True,
                    "config": {
                        "default_provider": "local",
                        "providers": {
                            "s3": {
                                "bucket": "bucket",
                                "access_key_id": "key",
                                "secret_access_key": "secret",
                            }
                        },
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "selected_providers": ["stripe"],
            "summary": {"total": 1, "passed": 1, "failed": 0},
        },
    )

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "providers",
        "code": "certification_missing_provider",
        "message": "provider certification report does not cover: mysql, openai-speech",
    } in report["issues"]


def test_release_check_validates_all_configured_real_provider_credentials():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {
                            "openai": {"api_key": "sk-test"},
                            "anthropic": {},
                            "gemini": {},
                        },
                    },
                },
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {"openai": {}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "ai",
        "code": "api_key_required",
        "message": "anthropic AI provider requires api_key in production config",
    } in report["issues"]
    assert {
        "severity": "error",
        "plugin": "ai",
        "code": "api_key_required",
        "message": "gemini AI provider requires api_key in production config",
    } in report["issues"]
    assert any(
        issue["plugin"] == "speech" and issue["code"] == "openai_config_invalid"
        for issue in report["issues"]
    )


def test_release_check_validates_all_configured_real_payment_and_notification_configs():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "connect_on_startup": True,
                        "config": {"redis_enabled": False},
                    },
                },
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {"stripe": {"api_key": "sk-test"}},
                    },
                },
                "notifications": {
                    "enabled": True,
                    "config": {
                        "default_provider": "smtp",
                        "health_probe": True,
                        "providers": {"smtp": {"sender": "n@example.com"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "stripe_webhook_secret_required",
        "message": "Stripe production config should include webhook_secret",
    } in report["issues"]
    assert any(
        issue["plugin"] == "notifications" and issue["code"] == "smtp_config_invalid"
        for issue in report["issues"]
    )


def test_release_check_accepts_hardened_webhook_notifications_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "notifications": {
                    "enabled": True,
                    "config": {
                        "default_provider": "webhook",
                        "health_probe": True,
                        "providers": {
                            "webhook": {
                                "url": "https://hooks.example.test/notify",
                                "health_url": "https://hooks.example.test/health",
                                "signing_secret": "hook-secret-at-least-runtime-owned",
                            }
                        },
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_requires_webhook_notifications_signing_secret_and_health_url():
    settings = InfraSettings(
        infra={
            "plugins": {
                "notifications": {
                    "enabled": True,
                    "config": {
                        "default_provider": "webhook",
                        "health_probe": True,
                        "providers": {
                            "webhook": {
                                "url": "https://hooks.example.test/notify",
                            }
                        },
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    codes = {(issue["plugin"], issue["code"]) for issue in report["issues"]}
    assert ("notifications", "webhook_signing_secret_required") in codes
    assert ("notifications", "webhook_health_url_required") in codes


def test_release_check_blocks_providers_without_builtin_certification():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                        "providers": {
                            "openai": {"api_key": "sk-test"},
                            "acme": {"api_key": "secret"},
                        },
                    },
                },
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "mock",
                        "providers": {"adyen": {"api_key": "secret"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "ai",
        "code": "uncertified_provider",
        "message": "production ai provider is not covered by the active certification catalog: acme",
    } in report["issues"]
    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "uncertified_provider",
        "message": (
            "production payment provider is not covered by the active certification catalog: adyen"
        ),
    } in report["issues"]


def test_release_check_accepts_external_provider_certification_catalog(monkeypatch):
    import infra.plugins.provider_extensions as provider_extensions

    monkeypatch.setattr(
        provider_extensions,
        "entry_points",
        lambda group: [FakeProviderEntryPoint()] if group == "fastapi_infra.ai_providers" else [],
    )
    acme_check = ProviderCheck(
        "acme-ai",
        ("test_live_acme_chat",),
        required_env=("ACME_API_KEY",),
        required_packages=("acme-sdk",),
        test_path="tests/integration/test_acme_live.py",
        provider_kind="ai",
        provider_name="acme",
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "acme",
                        "health_probe": True,
                        "providers": {
                            "acme": {"api_key": "secret"},
                        },
                    },
                },
            }
        }
    )
    provider_report = {
        "certified": True,
        "generated_at": _fresh_timestamp(),
        "test_path": None,
        "test_paths": ["tests/integration/test_acme_live.py"],
        "selected_providers": ["acme-ai"],
        "summary": {
            "total": 1,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "missing": 0,
        },
        "providers": [
            _passed_custom_provider(
                "acme-ai",
                test_path="tests/integration/test_acme_live.py",
                tests=("test_live_acme_chat",),
                required_env=("ACME_API_KEY",),
                required_packages=("acme-sdk",),
            )
        ],
    }

    report = build_release_check_report(
        settings,
        provider_certification_report=provider_report,
        provider_checks=(*DEFAULT_PROVIDER_CHECKS, acme_check),
    )

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_validates_enabled_plugin_migrations(tmp_path, monkeypatch):
    import infra.plugins.provider_extensions as provider_extensions

    monkeypatch.setattr(
        provider_extensions,
        "entry_points",
        lambda group: [],
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "connect_on_startup": True,
                        "config": {"mysql_enabled": True, "redis_enabled": False},
                    },
                },
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
                "webhooks": _stripe_webhooks_plugin(),
            }
        }
    )
    (tmp_path / "00000000001000_infra_payment_store.sql").write_text(
        "CREATE TABLE infra_payment_checkouts (id VARCHAR(64));\n",
        encoding="utf-8",
    )
    (tmp_path / "00000000001100_infra_webhook_store.sql").write_text(
        "CREATE TABLE infra_webhook_events (id VARCHAR(128));\n",
        encoding="utf-8",
    )

    report = build_release_check_report(
        settings,
        provider_certification_report=_passed_certification_report(["mysql", "stripe"]),
        migrations_path=tmp_path,
    )

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_reports_missing_enabled_plugin_migrations(tmp_path):
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "mock",
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        require_provider_certification=False,
        migrations_path=tmp_path,
    )

    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "migration_missing",
        "message": (
            "required plugin migration is missing: " "00000000001000_infra_payment_store.sql"
        ),
    } in report["issues"]


def test_release_check_requires_external_provider_certification_test_path(monkeypatch):
    import infra.plugins.provider_extensions as provider_extensions

    monkeypatch.setattr(
        provider_extensions,
        "entry_points",
        lambda group: [FakeProviderEntryPoint()] if group == "fastapi_infra.ai_providers" else [],
    )
    acme_check = ProviderCheck(
        "acme-ai",
        ("test_live_acme_chat",),
        test_path="tests/integration/test_acme_live.py",
        provider_kind="ai",
        provider_name="acme",
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "acme",
                        "health_probe": True,
                        "providers": {"acme": {"api_key": "secret"}},
                    },
                },
            }
        }
    )
    provider_report = {
        "certified": True,
        "generated_at": _fresh_timestamp(),
        "test_path": DEFAULT_LIVE_PROVIDER_TEST_PATH,
        "selected_providers": ["acme-ai"],
        "summary": {
            "total": 1,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "missing": 0,
        },
        "providers": [
            _passed_custom_provider(
                "acme-ai",
                test_path=DEFAULT_LIVE_PROVIDER_TEST_PATH,
                tests=("test_live_acme_chat",),
            )
        ],
    }

    report = build_release_check_report(
        settings,
        provider_certification_report=provider_report,
        provider_checks=(*DEFAULT_PROVIDER_CHECKS, acme_check),
    )

    assert {
        "severity": "error",
        "plugin": "providers",
        "code": "certification_invalid",
        "message": (
            "provider certification report does not cover required test paths: "
            "tests/integration/test_acme_live.py"
        ),
    } in report["issues"]


def test_release_check_requires_external_health_probe():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert report["issues"][0]["code"] == "health_probe_required"


def test_release_check_uses_shared_database_redis_setting_for_cache():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "config": {"redis_enabled": False},
                    },
                },
                "cache": {
                    "enabled": True,
                    "config": {
                        "default_provider": "redis",
                        "database_config": {"redis_enabled": True},
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "cache",
        "code": "redis_required",
        "message": "production cache requires Redis to be enabled",
    } in report["issues"]


def test_release_check_requires_redis_backing_for_redis_tasks():
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {"default_provider": "redis"},
                }
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "tasks",
        "code": "redis_backing_required",
        "message": "Redis task provider requires a Redis client or an enabled database plugin",
    } in report["issues"]


def test_release_check_requires_database_redis_enabled_for_redis_tasks():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "config": {"redis_enabled": False},
                    },
                },
                "tasks": {
                    "enabled": True,
                    "config": {"default_provider": "redis"},
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "tasks",
        "code": "redis_backing_required",
        "message": "Redis task provider requires database.config.redis_enabled=true",
    } in report["issues"]


def test_release_check_requires_redis_certification_for_redis_tasks():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "connect_on_startup": True,
                        "config": {"mysql_enabled": False, "redis_enabled": True},
                    },
                },
                "tasks": {
                    "enabled": True,
                    "config": {"default_provider": "redis"},
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report={
            "certified": True,
            "selected_providers": ["mysql"],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "missing": 0},
            "providers": [_passed_provider("mysql")],
        },
    )

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "providers",
        "code": "certification_missing_provider",
        "message": "provider certification report does not cover: redis",
    } in report["issues"]


@pytest.mark.parametrize(
    "provider_name, provider_config",
    [
        ("sqs", {"queue_url": "https://sqs.us-east-1.amazonaws.com/123/tasks"}),
        (
            "kafka",
            {
                "bootstrap_servers": "localhost:9092",
                "topic": "tasks",
                "group_id": "workers",
            },
        ),
        ("celery", {"broker_url": "redis://localhost:6379/0"}),
    ],
)
def test_release_check_accepts_builtin_durable_task_backends(provider_name, provider_config):
    settings = InfraSettings(
        infra={
            "plugins": {
                "tasks": {
                    "enabled": True,
                    "config": {
                        "default_provider": provider_name,
                        "providers": {provider_name: provider_config},
                    },
                }
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_accepts_hardened_webhook_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "webhooks": {
                    "enabled": True,
                    "config": {
                        "durable_store": True,
                        "providers": {"stripe": {"webhook_secret": "whsec_test"}},
                        "required_providers": ["stripe"],
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_requires_declared_webhook_providers():
    settings = InfraSettings(
        infra={
            "plugins": {
                "webhooks": {
                    "enabled": True,
                    "config": {
                        "durable_store": True,
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "webhooks",
        "code": "providers_required",
        "message": "production webhook routes should declare signed providers",
    } in report["issues"]


def test_release_check_requires_stripe_webhook_provider_when_payment_uses_stripe():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
                "webhooks": {
                    "enabled": True,
                    "config": {
                        "durable_store": True,
                        "providers": {},
                        "required_providers": [],
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report=_passed_certification_report(["stripe"]),
    )

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "stripe_webhook_provider_required",
        "message": "Stripe payment requires webhooks.providers.stripe",
    } in report["issues"]
    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "stripe_webhook_required_provider_required",
        "message": "Stripe payment requires webhooks.required_providers to include stripe",
    } in report["issues"]


def test_release_check_requires_enabled_webhooks_when_payment_uses_stripe():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report=_passed_certification_report(["stripe"]),
    )

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "stripe_webhook_provider_required",
        "message": "Stripe payment requires webhooks.providers.stripe",
    } in report["issues"]


def test_release_check_requires_required_stripe_webhook_provider():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
                "webhooks": {
                    "enabled": True,
                    "config": {
                        "durable_store": True,
                        "providers": {"stripe": {"webhook_secret": "whsec_test"}},
                        "required_providers": [],
                    },
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report=_passed_certification_report(["stripe"]),
    )

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "stripe_webhook_required_provider_required",
        "message": "Stripe payment requires webhooks.required_providers to include stripe",
    } in report["issues"]


def test_release_check_does_not_require_stripe_webhooks_for_mock_payment():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "mock",
                        "providers": {},
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert not any(
        issue["plugin"] == "payment" and issue["code"].startswith("stripe_webhook")
        for issue in report["issues"]
    )


def test_release_check_requires_database_payment_store_to_have_mysql():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "connect_on_startup": True,
                        "config": {"mysql_enabled": False, "redis_enabled": True},
                    },
                },
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "durable_database_store_required",
        "message": "payment store_service='database' requires MySQL to be enabled",
    } in report["issues"]


def test_release_check_requires_enabled_database_for_database_payment_store():
    settings = InfraSettings(
        infra={
            "plugins": {
                "payment": {
                    "enabled": True,
                    "config": {
                        "default_provider": "stripe",
                        "health_probe": True,
                        "store_service": "database",
                        "providers": {
                            "stripe": {
                                "api_key": "sk-test",
                                "webhook_secret": "whsec_test",
                            }
                        },
                    },
                },
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "durable_database_store_required",
        "message": "payment store_service='database' requires the database plugin to be enabled",
    } in report["issues"]


def test_release_check_warns_for_local_observability_backends():
    settings = InfraSettings(
        infra={
            "plugins": {
                "observability": {
                    "enabled": True,
                    "config": {
                        "metrics_backend": "memory",
                        "tracing_backend": "none",
                    },
                }
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is True
    assert report["summary"] == {"errors": 0, "warnings": 2}
    assert {
        "severity": "warning",
        "plugin": "observability",
        "code": "memory_metrics",
        "message": "production observability should use metrics_backend='prometheus'",
    } in report["issues"]
    assert {
        "severity": "warning",
        "plugin": "observability",
        "code": "tracing_disabled",
        "message": "production observability should configure tracing_backend='opentelemetry'",
    } in report["issues"]


def test_release_check_accepts_production_observability_backends():
    settings = InfraSettings(
        infra={
            "plugins": {
                "observability": {
                    "enabled": True,
                    "config": {
                        "metrics_backend": "prometheus",
                        "tracing_backend": "opentelemetry",
                    },
                }
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_blocks_memory_rate_limit_provider():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ratelimit": {
                    "enabled": True,
                    "config": {"default_provider": "memory"},
                }
            }
        }
    )

    report = build_release_check_report(settings)

    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "ratelimit",
            "code": "memory_provider",
            "message": "production rate limiting cannot use the in-memory provider",
        }
    ]


def test_release_check_accepts_redis_rate_limit_provider_with_redis_database():
    settings = InfraSettings(
        infra={
            "plugins": {
                "database": {
                    "enabled": True,
                    "config": {
                        "default_provider": "connections",
                        "connect_on_startup": True,
                        "config": {
                            "mysql_enabled": False,
                            "redis_enabled": True,
                        },
                    },
                },
                "ratelimit": {
                    "enabled": True,
                    "config": {"default_provider": "redis"},
                },
            }
        }
    )

    report = build_release_check_report(
        settings,
        provider_certification_report=_passed_certification_report(["redis"]),
    )

    assert report["ready"] is True
    assert report["issues"] == []


def test_release_check_requires_redis_backing_for_redis_rate_limit_provider():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ratelimit": {
                    "enabled": True,
                    "config": {"default_provider": "redis"},
                }
            }
        }
    )

    report = build_release_check_report(settings, require_provider_certification=False)

    assert report["ready"] is False
    assert {
        "severity": "error",
        "plugin": "ratelimit",
        "code": "redis_backing_required",
        "message": "Redis rate limiting requires a Redis client or an enabled database plugin",
    } in report["issues"]


def test_release_check_redacts_secrets_from_validation_errors():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "jwt_secret": {"password": "jwt-secret-value"},
                    },
                }
            }
        }
    )

    report = build_release_check_report(settings)
    message = report["issues"][0]["message"]

    assert "jwt-secret-value" not in message
    assert "[redacted]" in message


def test_release_check_redacts_secrets_from_plugin_release_check_mappings():
    settings = InfraSettings(infra={"plugins": {"secret_mapping_external": {"enabled": True}}})

    report = build_release_check_report(
        settings,
        plugins=[SecretMappingReleaseCheckPlugin()],
        require_provider_certification=False,
    )

    message = report["issues"][0]["message"]
    assert "real-secret-value" not in message
    assert "[redacted]" in message


def test_live_provider_workflow_runs_release_check_with_certification_report():
    workflow = Path(".github/workflows/live-providers.yml").read_text(encoding="utf-8")

    assert "provider-release-settings.json" in workflow
    assert "--provider-certification-report provider-certification.json" in workflow
    assert "--require-provider-certification" not in workflow
    assert "release-check.json" in workflow


def test_live_provider_workflow_certifies_mysql_with_stripe_payment_store():
    workflow = Path(".github/workflows/live-providers.yml").read_text(encoding="utf-8")

    assert "from infra.provider_certification import selected_checks" in workflow
    assert '{check.name for check in selected_checks(["${{ inputs.provider }}"])}' in workflow
    assert "provider_args+=(--provider mysql)" not in workflow
