from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeGuard

from pydantic import ValidationError

from infra.config import validate_infra_settings
from infra.config.models import InfraSettings, PluginSettings
from infra.core.health import redact_secret_text
from infra.database.migrations import MigrationError, load_sql_migrations
from infra.plugins.contract import (
    PluginProviderCertificationHook,
    PluginProviderPolicyHook,
    PluginReleaseCheckHook,
    PluginReleaseDependencyHook,
    resolve_plugin_manifest_hints,
)
from infra.provider_certification import (
    ProviderCheck,
    expand_provider_check_names,
    get_provider_checks,
)

Severity = Literal["error", "warning"]
PROVIDER_CERTIFICATION_MAX_AGE = timedelta(hours=24)
PROVIDER_CERTIFICATION_MAX_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class ReleaseCheckIssue:
    plugin: str
    code: str
    message: str
    severity: Severity = "error"


def evaluate_release_readiness(
    settings: InfraSettings,
    *,
    provider_certification_report: Mapping[str, Any] | None = None,
    require_provider_certification: bool = True,
    plugins: Iterable[Any] | None = None,
    provider_checks: Iterable[ProviderCheck] | None = None,
    migrations_path: str | Path | None = None,
) -> list[ReleaseCheckIssue]:
    issues: list[ReleaseCheckIssue] = []
    plugin_registry = tuple(plugins if plugins is not None else _get_builtin_plugins())
    provider_check_catalog = _provider_check_catalog(provider_checks)
    provider_checks_by_name = {check.name: check for check in provider_check_catalog}
    provider_check_map = _provider_check_map(provider_check_catalog)
    expected_provider_certifications = _expected_provider_certifications(
        settings,
        provider_check_catalog,
        plugin_registry,
        issues,
    )

    _check_plugin_provider_policies(settings, plugin_registry, provider_check_map, issues)
    _check_plugin_release_dependencies(settings, plugin_registry, issues)
    _check_plugin_release_checks(settings, plugin_registry, issues)
    _check_provider_certification(
        provider_certification_report,
        expected_provider_certifications,
        require_provider_certification,
        provider_checks_by_name,
        issues,
    )
    _check_plugin_schema(settings, plugin_registry, issues)
    _check_plugin_migrations(settings, plugin_registry, migrations_path, issues)

    return issues


def _get_builtin_plugins() -> Iterable[Any]:
    from infra.plugins.builtin import get_builtin_plugins

    return get_builtin_plugins()


def _provider_check_catalog(
    provider_checks: Iterable[ProviderCheck] | None,
) -> tuple[ProviderCheck, ...]:
    if provider_checks is not None:
        return tuple(provider_checks)
    return get_provider_checks()


def build_release_check_report(
    settings: InfraSettings,
    *,
    provider_certification_report: Mapping[str, Any] | None = None,
    require_provider_certification: bool = True,
    plugins: Iterable[Any] | None = None,
    provider_checks: Iterable[ProviderCheck] | None = None,
    migrations_path: str | Path | None = None,
) -> dict[str, Any]:
    issues = evaluate_release_readiness(
        settings,
        provider_certification_report=provider_certification_report,
        require_provider_certification=require_provider_certification,
        plugins=plugins,
        provider_checks=provider_checks,
        migrations_path=migrations_path,
    )
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    return {
        "ready": not errors,
        "summary": {
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "issues": [
            {
                "severity": issue.severity,
                "plugin": issue.plugin,
                "code": issue.code,
                "message": issue.message,
            }
            for issue in issues
        ],
    }


def format_release_check_report(
    settings: InfraSettings,
    *,
    provider_certification_report: Mapping[str, Any] | None = None,
    require_provider_certification: bool = True,
    plugins: Iterable[Any] | None = None,
    provider_checks: Iterable[ProviderCheck] | None = None,
    migrations_path: str | Path | None = None,
) -> str:
    return format_release_check_json(
        build_release_check_report(
            settings,
            provider_certification_report=provider_certification_report,
            require_provider_certification=require_provider_certification,
            plugins=plugins,
            provider_checks=provider_checks,
            migrations_path=migrations_path,
        )
    )


def format_release_check_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def format_release_check_text(
    settings: InfraSettings,
    *,
    provider_certification_report: Mapping[str, Any] | None = None,
    require_provider_certification: bool = True,
    plugins: Iterable[Any] | None = None,
    provider_checks: Iterable[ProviderCheck] | None = None,
    migrations_path: str | Path | None = None,
) -> str:
    return format_release_check_text_report(
        build_release_check_report(
            settings,
            provider_certification_report=provider_certification_report,
            require_provider_certification=require_provider_certification,
            plugins=plugins,
            provider_checks=provider_checks,
            migrations_path=migrations_path,
        )
    )


def format_release_check_text_report(report: Mapping[str, Any]) -> str:
    lines = [
        "release-check: ready" if report["ready"] else "release-check: blocked",
        f"errors: {report['summary']['errors']}",
        f"warnings: {report['summary']['warnings']}",
    ]
    for issue in report["issues"]:
        lines.append(f"{issue['severity']} {issue['plugin']}.{issue['code']}: {issue['message']}")
    return "\n".join(lines)


def expected_provider_check_names(
    settings: InfraSettings,
    *,
    plugins: Iterable[Any] | None = None,
    provider_checks: Iterable[ProviderCheck] | None = None,
) -> tuple[str, ...]:
    plugin_registry = tuple(plugins if plugins is not None else _get_builtin_plugins())
    provider_check_catalog = _provider_check_catalog(provider_checks)
    issues: list[ReleaseCheckIssue] = []
    expected = _expected_provider_certifications(
        settings,
        provider_check_catalog,
        plugin_registry,
        issues,
    )
    if issues:
        messages = "; ".join(f"{issue.plugin}.{issue.code}: {issue.message}" for issue in issues)
        raise ValueError(messages)
    return tuple(check.name for check in provider_check_catalog if check.name in expected)


def _check_provider_certification(
    report: Mapping[str, Any] | None,
    expected_providers: set[str],
    require_provider_certification: bool,
    provider_checks: Mapping[str, ProviderCheck],
    issues: list[ReleaseCheckIssue],
) -> None:
    if report is None:
        if require_provider_certification and expected_providers:
            _error(
                issues,
                "providers",
                "certification_report_required",
                "provider certification report is required for: "
                + ", ".join(sorted(expected_providers)),
            )
        return
    if report.get("certified") is not True:
        _error(
            issues,
            "providers",
            "certification_not_passed",
            "provider certification report is not certified",
        )
        return
    selected_providers = report.get("selected_providers")
    if not isinstance(selected_providers, list):
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report does not list selected providers",
        )
        return
    if not _check_selected_provider_entries(selected_providers, provider_checks, issues):
        return
    selected_provider_names = {
        provider for provider in selected_providers if isinstance(provider, str)
    }
    missing_providers = expected_providers - {
        provider for provider in selected_providers if isinstance(provider, str)
    }
    if missing_providers:
        _error(
            issues,
            "providers",
            "certification_missing_provider",
            "provider certification report does not cover: " + ", ".join(sorted(missing_providers)),
        )
        return
    summary = report.get("summary")
    if not isinstance(summary, Mapping) or not summary.get("total"):
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report has no provider results",
        )
        return
    if (
        summary.get("passed") != summary.get("total")
        or summary.get("failed", 0) != 0
        or summary.get("skipped", 0) != 0
        or summary.get("missing", 0) != 0
    ):
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification summary does not show all providers passed",
        )
        return
    if not _check_certification_freshness(report, issues):
        return
    if not _check_certification_test_paths(
        report, provider_checks, selected_provider_names, issues
    ):
        return
    _check_expected_provider_results(
        report,
        expected_providers or selected_provider_names,
        provider_checks,
        issues,
    )


def _check_expected_provider_results(
    report: Mapping[str, Any],
    expected_providers: set[str],
    provider_checks: Mapping[str, ProviderCheck],
    issues: list[ReleaseCheckIssue],
) -> None:
    providers = report.get("providers")
    if not isinstance(providers, list):
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report does not include provider results",
        )
        return
    if not _check_provider_result_summary(report, providers, issues):
        return
    provider_results = {
        provider.get("name"): provider for provider in providers if isinstance(provider, Mapping)
    }
    duplicate_results = sorted(_duplicate_provider_result_names(providers))
    if duplicate_results:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report has duplicate provider results for: "
            + ", ".join(duplicate_results),
        )
        return
    if not _check_selected_provider_results(report, providers, issues):
        return
    missing_results = sorted(expected_providers - set(provider_results))
    if missing_results:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report is missing results for: " + ", ".join(missing_results),
        )
        return
    missing_tests = [
        provider
        for provider in sorted(expected_providers)
        if _provider_missing_required_tests(provider_results[provider], provider, provider_checks)
    ]
    if missing_tests:
        _error(
            issues,
            "providers",
            "certification_provider_tests_missing",
            "provider certification report is missing required test evidence for: "
            + ", ".join(missing_tests),
        )
        return
    incomplete_requirements = [
        provider
        for provider in sorted(expected_providers)
        if _provider_requirements_incomplete(provider_results[provider], provider, provider_checks)
    ]
    if incomplete_requirements:
        _error(
            issues,
            "providers",
            "certification_provider_requirements_incomplete",
            "provider certification report is missing required requirement metadata for: "
            + ", ".join(incomplete_requirements),
        )
        return
    unmet_requirements = [
        provider
        for provider in sorted(expected_providers)
        if _provider_has_unmet_requirements(provider_results[provider])
    ]
    if unmet_requirements:
        _error(
            issues,
            "providers",
            "certification_provider_requirements_missing",
            "provider certification report has unmet requirements for: "
            + ", ".join(unmet_requirements),
        )


def _check_selected_provider_entries(
    selected_providers: list[Any],
    provider_checks: Mapping[str, ProviderCheck],
    issues: list[ReleaseCheckIssue],
) -> bool:
    if any(not isinstance(provider, str) for provider in selected_providers):
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report has invalid selected providers",
        )
        return False
    duplicates = sorted(_duplicate_strings(selected_providers))
    if duplicates:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report has duplicate selected providers: "
            + ", ".join(duplicates),
        )
        return False
    unknown = sorted(set(selected_providers) - set(provider_checks))
    if unknown:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report has unknown selected providers: " + ", ".join(unknown),
        )
        return False
    return True


def _check_selected_provider_results(
    report: Mapping[str, Any],
    providers: list[Any],
    issues: list[ReleaseCheckIssue],
) -> bool:
    selected_providers = report.get("selected_providers")
    if not isinstance(selected_providers, list):
        return False
    selected = {provider for provider in selected_providers if isinstance(provider, str)}
    provider_names = {
        provider.get("name")
        for provider in providers
        if isinstance(provider, Mapping) and isinstance(provider.get("name"), str)
    }
    if selected != provider_names:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification selected providers do not match provider results",
        )
        return False
    return True


def _check_provider_result_summary(
    report: Mapping[str, Any],
    providers: list[Any],
    issues: list[ReleaseCheckIssue],
) -> bool:
    counts = _provider_result_counts(providers)
    if counts is None:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report has invalid provider result entries",
        )
        return False
    summary = report.get("summary")
    if not isinstance(summary, Mapping) or any(
        summary.get(key) != value for key, value in counts.items()
    ):
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification summary does not match provider results",
        )
        return False
    return True


def _provider_result_counts(providers: list[Any]) -> dict[str, int] | None:
    counts = {"total": len(providers), "passed": 0, "failed": 0, "skipped": 0, "missing": 0}
    for provider in providers:
        if not isinstance(provider, Mapping) or not isinstance(provider.get("name"), str):
            return None
        outcome = provider.get("outcome")
        if outcome not in {"passed", "failed", "skipped", "missing"}:
            return None
        counts[outcome] += 1
    return counts


def _duplicate_provider_result_names(providers: list[Any]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for provider in providers:
        if not isinstance(provider, Mapping):
            continue
        name = provider.get("name")
        if not isinstance(name, str):
            continue
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    return duplicates


def _duplicate_strings(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _provider_missing_required_tests(
    provider: Mapping[str, Any],
    provider_name: str,
    provider_checks: Mapping[str, ProviderCheck],
) -> bool:
    check = provider_checks[provider_name]
    tests = provider.get("tests")
    if not isinstance(tests, list):
        return True
    reported_tests = {test for test in tests if isinstance(test, str)}
    if not set(check.tests).issubset(reported_tests):
        return True
    details = provider.get("details")
    if not isinstance(details, list):
        return True
    detail_lines = [detail for detail in details if isinstance(detail, str)]
    return any(
        not any(
            _provider_detail_matches_test(detail, test_name, check.test_path)
            for detail in detail_lines
        )
        for test_name in check.tests
    )


def _provider_detail_matches_test(detail: str, test_name: str, test_path: str) -> bool:
    nodeid = detail.split(": ", 1)[0]
    path = nodeid.split("::", 1)[0]
    if path != test_path:
        return False
    item_name = nodeid.rsplit("::", 1)[-1]
    base_name = item_name.split("[", 1)[0]
    return base_name == test_name


def _provider_requirements_incomplete(
    provider: Mapping[str, Any],
    provider_name: str,
    provider_checks: Mapping[str, ProviderCheck],
) -> bool:
    check = provider_checks[provider_name]
    requirements = provider.get("requirements")
    if not isinstance(requirements, Mapping):
        return True
    return not _contains_all(
        requirements.get("required_env"), check.required_env
    ) or not _contains_all(
        requirements.get("required_packages"),
        check.required_packages,
    )


def _contains_all(value: Any, required: tuple[str, ...]) -> bool:
    if not isinstance(value, list):
        return False
    values = {item for item in value if isinstance(item, str)}
    return set(required).issubset(values)


def _provider_has_unmet_requirements(provider: Mapping[str, Any]) -> bool:
    requirements = provider.get("requirements")
    if not isinstance(requirements, Mapping):
        return True
    missing_env = requirements.get("missing_required_env")
    missing_packages = requirements.get("missing_required_packages")
    if not _string_list(missing_env) or not _string_list(missing_packages):
        return True
    return bool(missing_env or missing_packages)


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_certification_freshness(
    report: Mapping[str, Any],
    issues: list[ReleaseCheckIssue],
) -> bool:
    generated_at = report.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report is missing generated_at",
        )
        return False
    try:
        generated_at_time = _parse_utc_timestamp(generated_at)
    except ValueError:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report has invalid generated_at",
        )
        return False
    now = datetime.now(UTC)
    if generated_at_time - now > PROVIDER_CERTIFICATION_MAX_CLOCK_SKEW:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report generated_at is in the future",
        )
        return False
    if now - generated_at_time > PROVIDER_CERTIFICATION_MAX_AGE:
        _error(
            issues,
            "providers",
            "certification_stale",
            "provider certification report is older than 24 hours",
        )
        return False
    return True


def _parse_utc_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def _check_certification_test_paths(
    report: Mapping[str, Any],
    provider_checks: Mapping[str, ProviderCheck],
    selected_providers: set[str],
    issues: list[ReleaseCheckIssue],
) -> bool:
    reported_paths = _reported_certification_test_paths(report)
    if reported_paths is None:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report must include test_path or test_paths",
        )
        return False
    required_paths = {
        provider_checks[provider].test_path
        for provider in selected_providers
        if provider in provider_checks
    }
    missing_paths = sorted(required_paths - reported_paths)
    if missing_paths:
        _error(
            issues,
            "providers",
            "certification_invalid",
            "provider certification report does not cover required test paths: "
            + ", ".join(missing_paths),
        )
        return False
    return True


def _reported_certification_test_paths(report: Mapping[str, Any]) -> set[str] | None:
    test_paths = report.get("test_paths")
    if isinstance(test_paths, list):
        paths = {path for path in test_paths if isinstance(path, str) and path}
        if len(paths) == len(test_paths):
            return paths
        return None
    test_path = report.get("test_path")
    if isinstance(test_path, str) and test_path:
        return {test_path}
    return None


def _has_release_check_hook(plugin: Any) -> TypeGuard[PluginReleaseCheckHook]:
    return callable(getattr(plugin, "release_check", None))


def _has_release_dependency_hook(plugin: Any) -> TypeGuard[PluginReleaseDependencyHook]:
    return callable(getattr(plugin, "release_dependencies", None))


def _has_provider_certification_hook(
    plugin: Any,
) -> TypeGuard[PluginProviderCertificationHook]:
    return callable(getattr(plugin, "provider_certifications", None))


def _has_provider_policy_hook(plugin: Any) -> TypeGuard[PluginProviderPolicyHook]:
    return callable(getattr(plugin, "provider_release_policies", None))


def _expected_provider_certifications(
    settings: InfraSettings,
    provider_checks: Iterable[ProviderCheck],
    plugins: Iterable[Any],
    issues: list[ReleaseCheckIssue],
) -> set[str]:
    provider_check_catalog = tuple(provider_checks)
    provider_check_map = _provider_check_map(provider_check_catalog)
    expected: set[str] = set()
    for plugin in plugins:
        plugin_name = plugin.metadata.name
        setting = settings.infra.plugins.get(plugin_name)
        if not _enabled(setting):
            continue
        if not _has_provider_certification_hook(plugin):
            continue
        config = _validated_release_check_config(plugin, setting)
        if config is _INVALID_PLUGIN_CONFIG:
            continue
        for certification in plugin.provider_certifications(settings, config) or ():
            provider_ref = _coerce_plugin_provider_certification(certification)
            if provider_ref is None:
                _error(
                    issues,
                    plugin_name,
                    "provider_certification_invalid",
                    "plugin provider_certifications must return provider certification mappings",
                )
                break
            if (provider_check := provider_check_map.get(provider_ref)) is not None:
                expected.add(provider_check)

    return set(expand_provider_check_names(expected, checks=provider_check_catalog))


def _provider_check_map(
    provider_checks: Iterable[ProviderCheck],
) -> dict[tuple[str, str], str]:
    return {
        (check.provider_kind, check.provider_name): check.name
        for check in provider_checks
        if check.provider_kind is not None and check.provider_name is not None
    }


def _certified_provider_names(
    provider_check_map: Mapping[tuple[str, str], str],
    provider_kind: str,
) -> set[str]:
    return {name for (kind, name) in provider_check_map if kind == provider_kind}


def _check_plugin_provider_policies(
    settings: InfraSettings,
    plugins: Iterable[Any],
    provider_check_map: Mapping[tuple[str, str], str],
    issues: list[ReleaseCheckIssue],
) -> None:
    for plugin in plugins:
        plugin_name = plugin.metadata.name
        setting = settings.infra.plugins.get(plugin_name)
        if not _enabled(setting):
            continue
        if not _has_provider_policy_hook(plugin):
            continue
        config = _validated_release_check_config(plugin, setting)
        if config is _INVALID_PLUGIN_CONFIG:
            continue
        for policy in plugin.provider_release_policies(settings, config) or ():
            normalized_policy = _coerce_plugin_provider_policy(policy)
            if normalized_policy is None:
                _error(
                    issues,
                    plugin_name,
                    "provider_policy_invalid",
                    "plugin provider_release_policies must return provider policy mappings",
                )
                break
            provider_kind, declared_providers, local_providers, health_probe = normalized_policy
            certified_providers = _certified_provider_names(provider_check_map, provider_kind)
            _check_uncertified_provider_names(
                plugin_name,
                declared_providers,
                local_providers=local_providers,
                certified_providers=certified_providers,
                issues=issues,
            )
            if (declared_providers & certified_providers) - local_providers:
                _require_health_probe(plugin_name, health_probe, issues)


def _check_plugin_release_dependencies(
    settings: InfraSettings,
    plugins: Iterable[Any],
    issues: list[ReleaseCheckIssue],
) -> None:
    for plugin in plugins:
        plugin_name = plugin.metadata.name
        setting = settings.infra.plugins.get(plugin_name)
        if not _enabled(setting):
            continue
        if not _has_release_dependency_hook(plugin):
            continue

        config = _validated_release_check_config(plugin, setting)
        if config is _INVALID_PLUGIN_CONFIG:
            continue

        for dependency in plugin.release_dependencies(settings, config) or ():
            normalized = _coerce_plugin_release_dependency(plugin_name, dependency)
            if normalized is None:
                _error(
                    issues,
                    plugin_name,
                    "release_dependency_invalid",
                    "plugin release_dependencies must return dependency mappings",
                )
                break
            _check_plugin_release_dependency(settings, plugin_name, normalized, issues)


def _coerce_plugin_release_dependency(
    source_plugin: str,
    dependency: Any,
) -> dict[str, Any] | None:
    if not isinstance(dependency, Mapping):
        return None
    target_plugin = dependency.get("plugin")
    code = dependency.get("code")
    message = dependency.get("message")
    severity = dependency.get("severity", "error")
    config_path = dependency.get("config_path")
    has_contains = "contains" in dependency
    has_equals = "equals" in dependency

    if not isinstance(target_plugin, str) or not target_plugin:
        return None
    if not isinstance(code, str) or not code:
        return None
    if not isinstance(message, str) or not message:
        return None
    if severity not in {"error", "warning"}:
        return None
    if config_path is not None and (not isinstance(config_path, str) or not config_path.strip()):
        return None
    if has_contains and has_equals:
        return None

    normalized: dict[str, Any] = {
        "source_plugin": source_plugin,
        "target_plugin": target_plugin,
        "code": code,
        "message": message,
        "severity": severity,
    }
    if isinstance(config_path, str):
        normalized["config_path"] = config_path.strip()
    if has_contains:
        normalized["contains"] = dependency["contains"]
    if has_equals:
        normalized["equals"] = dependency["equals"]
    return normalized


def _check_plugin_release_dependency(
    settings: InfraSettings,
    source_plugin: str,
    dependency: Mapping[str, Any],
    issues: list[ReleaseCheckIssue],
) -> None:
    target_plugin = dependency["target_plugin"]
    setting = settings.infra.plugins.get(target_plugin)
    if not _enabled(setting):
        issues.append(
            ReleaseCheckIssue(
                plugin=source_plugin,
                code=dependency["code"],
                message=redact_secret_text(dependency["message"]),
                severity=dependency["severity"],
            )
        )
        return

    config_path = dependency.get("config_path")
    if not isinstance(config_path, str):
        return

    found, value = _lookup_config_path(setting.config, config_path)
    if "contains" in dependency:
        satisfied = _value_contains(value, dependency["contains"]) if found else False
    elif "equals" in dependency:
        satisfied = found and value == dependency["equals"]
    else:
        satisfied = found and value is not None

    if not satisfied:
        issues.append(
            ReleaseCheckIssue(
                plugin=source_plugin,
                code=dependency["code"],
                message=redact_secret_text(dependency["message"]),
                severity=dependency["severity"],
            )
        )


def _lookup_config_path(config: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = config
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _value_contains(value: Any, expected: Any) -> bool:
    if isinstance(value, Mapping):
        return expected in value
    if isinstance(value, str):
        return bool(value == expected)
    if isinstance(value, Iterable):
        return bool(expected in value)
    return False


def _check_uncertified_provider_names(
    plugin: str,
    declared_providers: set[str],
    *,
    local_providers: set[str],
    certified_providers: set[str],
    issues: list[ReleaseCheckIssue],
) -> None:
    uncertified = declared_providers - local_providers - certified_providers
    if uncertified:
        _error(
            issues,
            plugin,
            "uncertified_provider",
            "production "
            + plugin
            + " provider is not covered by the active certification catalog: "
            + ", ".join(sorted(uncertified)),
        )


def _enabled(setting: PluginSettings | None) -> TypeGuard[PluginSettings]:
    return setting is not None and setting.enabled is True


def _check_plugin_release_checks(
    settings: InfraSettings,
    plugins: Iterable[Any],
    issues: list[ReleaseCheckIssue],
) -> None:
    for plugin in plugins:
        plugin_name = plugin.metadata.name
        setting = settings.infra.plugins.get(plugin_name)
        if not _enabled(setting):
            continue
        if not _has_release_check_hook(plugin):
            continue

        config = _validated_release_check_config(plugin, setting)
        if config is _INVALID_PLUGIN_CONFIG:
            continue

        try:
            plugin_issues = plugin.release_check(settings, config)
        except Exception as exc:
            _error(
                issues,
                plugin_name,
                "release_check_failed",
                f"plugin release_check failed: {exc}",
            )
            continue
        if plugin_issues is None:
            continue
        for issue in plugin_issues:
            normalized_issue = _coerce_plugin_release_check_issue(plugin_name, issue)
            if normalized_issue is not None:
                issues.append(normalized_issue)
                continue
            _error(
                issues,
                plugin_name,
                "release_check_invalid",
                "plugin release_check must return ReleaseCheckIssue items or issue mappings",
            )
            break


def _coerce_plugin_release_check_issue(
    plugin_name: str,
    issue: Any,
) -> ReleaseCheckIssue | None:
    if isinstance(issue, ReleaseCheckIssue):
        return ReleaseCheckIssue(
            plugin=issue.plugin,
            code=issue.code,
            message=redact_secret_text(issue.message),
            severity=issue.severity,
        )
    if not isinstance(issue, Mapping):
        return None

    raw_plugin = issue.get("plugin", plugin_name)
    raw_code = issue.get("code")
    raw_message = issue.get("message")
    raw_severity = issue.get("severity", "error")
    if not isinstance(raw_plugin, str) or not raw_plugin:
        return None
    if not isinstance(raw_code, str) or not raw_code:
        return None
    if not isinstance(raw_message, str) or not raw_message:
        return None
    if raw_severity not in {"error", "warning"}:
        return None
    return ReleaseCheckIssue(
        plugin=raw_plugin,
        code=raw_code,
        message=redact_secret_text(raw_message),
        severity=raw_severity,
    )


def _coerce_plugin_provider_certification(certification: Any) -> tuple[str, str] | None:
    if not isinstance(certification, Mapping):
        return None
    provider_kind = certification.get("provider_kind")
    provider_name = certification.get("provider_name")
    if not isinstance(provider_kind, str) or not provider_kind:
        return None
    if not isinstance(provider_name, str) or not provider_name:
        return None
    return (provider_kind, provider_name)


def _coerce_plugin_provider_policy(
    policy: Any,
) -> tuple[str, set[str], set[str], bool] | None:
    if not isinstance(policy, Mapping):
        return None
    provider_kind = policy.get("provider_kind")
    declared_providers = policy.get("declared_providers")
    local_providers = policy.get("local_providers", [])
    health_probe = policy.get("health_probe")
    if not isinstance(provider_kind, str) or not provider_kind:
        return None
    if not isinstance(declared_providers, list) or not all(
        isinstance(provider, str) and provider for provider in declared_providers
    ):
        return None
    if not isinstance(local_providers, list) or not all(
        isinstance(provider, str) and provider for provider in local_providers
    ):
        return None
    if not isinstance(health_probe, bool):
        return None
    return (
        provider_kind,
        set(declared_providers),
        set(local_providers),
        health_probe,
    )


class _InvalidPluginConfig:
    pass


_INVALID_PLUGIN_CONFIG = _InvalidPluginConfig()


def _validated_release_check_config(plugin: Any, setting: PluginSettings) -> Any:
    config_model = getattr(plugin, "config_model", None)
    if config_model is None:
        return None
    try:
        return config_model.model_validate(setting.config)
    except (ValidationError, ValueError):
        return _INVALID_PLUGIN_CONFIG


def _check_plugin_schema(
    settings: InfraSettings,
    plugins: Iterable[Any],
    issues: list[ReleaseCheckIssue],
) -> None:
    for issue in validate_infra_settings(settings, plugins):
        code = "config_invalid" if issue.code == "invalid_config" else issue.code
        if code == "config_invalid" and _has_config_issue(issues, issue.plugin):
            continue
        if _has_issue(issues, issue.plugin, code):
            continue
        _error(issues, issue.plugin, code, issue.message)


def _check_plugin_migrations(
    settings: InfraSettings,
    plugins: Iterable[Any],
    migrations_path: str | Path | None,
    issues: list[ReleaseCheckIssue],
) -> None:
    if migrations_path is None:
        return
    try:
        migrations = load_sql_migrations(migrations_path, allow_empty=True)
    except MigrationError as exc:
        _error(issues, "migrations", "migrations_invalid", str(exc))
        return
    migration_keys = {(migration.version, migration.name) for migration in migrations}
    for plugin in plugins:
        plugin_name = plugin.metadata.name
        if not _enabled(settings.infra.plugins.get(plugin_name)):
            continue
        hints = resolve_plugin_manifest_hints(plugin)
        for migration in hints.migrations:
            if (migration.version, migration.name) in migration_keys:
                continue
            _error(
                issues,
                plugin_name,
                "migration_missing",
                (
                    "required plugin migration is missing: "
                    f"{migration.version}_{migration.name}.sql"
                ),
            )


def _has_config_issue(issues: list[ReleaseCheckIssue], plugin: str) -> bool:
    return any(
        issue.plugin == plugin
        and (issue.code == "config_invalid" or issue.code.endswith("_config_invalid"))
        for issue in issues
    )


def _has_issue(issues: list[ReleaseCheckIssue], plugin: str, code: str) -> bool:
    return any(issue.plugin == plugin and issue.code == code for issue in issues)


def _require_health_probe(
    plugin: str,
    health_probe: bool,
    issues: list[ReleaseCheckIssue],
) -> None:
    if not health_probe:
        _error(
            issues,
            plugin,
            "health_probe_required",
            "external provider must enable health_probe in production",
        )


def _error(
    issues: list[ReleaseCheckIssue],
    plugin: str,
    code: str,
    message: str,
) -> None:
    issues.append(
        ReleaseCheckIssue(
            plugin=plugin,
            code=code,
            message=redact_secret_text(message),
        )
    )
