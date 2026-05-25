from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal

from infra.config.loader import load_env_file

Outcome = Literal["passed", "failed", "skipped", "missing"]
DEFAULT_LIVE_PROVIDER_TEST_PATH = "tests/integration/test_live_providers.py"
PACKAGED_LIVE_PROVIDER_TEST_PATH = str(
    Path(__file__).with_name("provider_tests") / "test_live_providers.py"
)
PROVIDER_CHECK_ENTRY_POINT_GROUP = "fastapi_infra.provider_checks"

PACKAGE_IMPORT_NAMES = {
    "google-genai": "google.genai",
}


@dataclass(frozen=True)
class ProviderCheck:
    name: str
    tests: tuple[str, ...]
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    required_packages: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    test_path: str = DEFAULT_LIVE_PROVIDER_TEST_PATH
    provider_kind: str | None = None
    provider_name: str | None = None


@dataclass(frozen=True)
class ProviderCertificationResult:
    name: str
    outcome: Outcome
    tests: tuple[str, ...]
    details: tuple[str, ...]
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    required_packages: tuple[str, ...] = ()
    test_path: str = DEFAULT_LIVE_PROVIDER_TEST_PATH


DEFAULT_PROVIDER_CHECKS: tuple[ProviderCheck, ...] = (
    ProviderCheck(
        "mysql",
        ("test_live_mysql_database_manager_round_trip",),
        required_env=(
            "MYSQL_LIVE_HOST",
            "MYSQL_LIVE_USER",
            "MYSQL_LIVE_PASSWORD",
            "MYSQL_LIVE_DB",
        ),
        optional_env=("MYSQL_LIVE_PORT", "MYSQL_LIVE_CONNECT_TIMEOUT"),
        required_packages=("aiomysql",),
        provider_kind="database",
        provider_name="mysql",
    ),
    ProviderCheck(
        "redis",
        ("test_live_redis_cache_service_round_trip",),
        required_env=("REDIS_LIVE_URL",),
        optional_env=("REDIS_LIVE_CONNECT_TIMEOUT",),
        required_packages=("redis",),
        provider_kind="database",
        provider_name="redis",
    ),
    ProviderCheck(
        "stripe",
        (
            "test_live_stripe_checkout_session_creation",
            "test_live_stripe_checkout_persists_to_mysql_store",
            "test_live_stripe_webhook_signature_entrypoint",
        ),
        required_env=("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET"),
        optional_env=("STRIPE_API_BASE", "STRIPE_LIVE_TIMEOUT"),
        dependencies=("mysql",),
        provider_kind="payment",
        provider_name="stripe",
    ),
    ProviderCheck(
        "s3",
        ("test_live_s3_put_get_list_and_presign",),
        required_env=(
            "S3_LIVE_BUCKET",
            "S3_LIVE_REGION",
            "S3_LIVE_ACCESS_KEY_ID",
            "S3_LIVE_SECRET_ACCESS_KEY",
        ),
        optional_env=(
            "S3_LIVE_ENDPOINT_URL",
            "S3_LIVE_FORCE_PATH_STYLE",
            "S3_LIVE_PREFIX",
            "S3_LIVE_TIMEOUT",
        ),
        provider_kind="storage",
        provider_name="s3",
    ),
    ProviderCheck(
        "openai-ai",
        ("test_live_openai_chat_and_embedding",),
        required_env=("OPENAI_API_KEY", "OPENAI_LIVE_CHAT_MODEL", "OPENAI_LIVE_EMBEDDING_MODEL"),
        optional_env=("OPENAI_API_BASE", "OPENAI_LIVE_TIMEOUT"),
        required_packages=("openai",),
        provider_kind="ai",
        provider_name="openai",
    ),
    ProviderCheck(
        "anthropic-ai",
        ("test_live_anthropic_chat",),
        required_env=("ANTHROPIC_API_KEY", "ANTHROPIC_LIVE_CHAT_MODEL"),
        optional_env=("ANTHROPIC_API_BASE", "ANTHROPIC_LIVE_TIMEOUT"),
        required_packages=("anthropic",),
        provider_kind="ai",
        provider_name="anthropic",
    ),
    ProviderCheck(
        "gemini-ai",
        ("test_live_gemini_chat_and_embedding",),
        required_env=("GEMINI_API_KEY", "GEMINI_LIVE_CHAT_MODEL", "GEMINI_LIVE_EMBEDDING_MODEL"),
        optional_env=("GEMINI_API_BASE", "GEMINI_LIVE_TIMEOUT"),
        required_packages=("google-genai",),
        provider_kind="ai",
        provider_name="gemini",
    ),
    ProviderCheck(
        "openai-speech",
        (
            "test_live_openai_speech_transcription",
            "test_live_openai_speech_synthesis",
        ),
        required_env=("OPENAI_API_KEY",),
        optional_env=(
            "OPENAI_API_BASE",
            "OPENAI_ASR_MODEL",
            "OPENAI_TTS_MODEL",
            "OPENAI_VOICE",
            "OPENAI_SPEECH_TIMEOUT",
        ),
        provider_kind="speech",
        provider_name="openai",
    ),
    ProviderCheck(
        "smtp",
        ("test_live_smtp_notification_send",),
        required_env=("SMTP_LIVE_HOST", "SMTP_LIVE_SENDER", "SMTP_LIVE_RECIPIENT"),
        optional_env=(
            "SMTP_LIVE_PORT",
            "SMTP_LIVE_USERNAME",
            "SMTP_LIVE_PASSWORD",
            "SMTP_LIVE_USE_TLS",
            "SMTP_LIVE_TIMEOUT",
        ),
        provider_kind="notifications",
        provider_name="smtp",
    ),
)

PROVIDER_CHECK_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "stripe": ("mysql",),
}


def get_provider_checks(
    *,
    entry_points_loader: Callable[..., Iterable[Any]] | None = None,
) -> tuple[ProviderCheck, ...]:
    loaded_checks = tuple(_load_entry_point_provider_checks(entry_points_loader))
    checks = (*DEFAULT_PROVIDER_CHECKS, *loaded_checks)
    _validate_provider_check_catalog(checks)
    return checks


def _load_entry_point_provider_checks(
    entry_points_loader: Callable[..., Iterable[Any]] | None = None,
) -> tuple[ProviderCheck, ...]:
    loader = entry_points_loader or entry_points
    checks: list[ProviderCheck] = []
    for entry_point in loader(group=PROVIDER_CHECK_ENTRY_POINT_GROUP):
        loaded = entry_point.load()
        value = loaded() if callable(loaded) and not isinstance(loaded, ProviderCheck) else loaded
        checks.extend(_provider_checks_from_entry_point_value(entry_point.name, value))
    return tuple(checks)


def _provider_checks_from_entry_point_value(
    entry_point_name: str,
    value: object,
) -> tuple[ProviderCheck, ...]:
    if isinstance(value, ProviderCheck):
        return (value,)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        checks = tuple(value)
        if all(isinstance(check, ProviderCheck) for check in checks):
            return checks
    raise ValueError(
        f"{PROVIDER_CHECK_ENTRY_POINT_GROUP}:{entry_point_name} must load ProviderCheck "
        "or an iterable of ProviderCheck"
    )


def _validate_provider_check_catalog(checks: tuple[ProviderCheck, ...]) -> None:
    names = [check.name for check in checks]
    duplicate_names = sorted(_duplicates(names))
    if duplicate_names:
        raise ValueError("duplicate provider check name: " + ", ".join(duplicate_names))
    provider_identities = [
        (check.provider_kind, check.provider_name)
        for check in checks
        if check.provider_kind is not None and check.provider_name is not None
    ]
    duplicate_identities = sorted(_duplicates(provider_identities))
    if duplicate_identities:
        formatted = ", ".join(f"{kind}:{name}" for kind, name in duplicate_identities)
        raise ValueError("duplicate provider certification identity: " + formatted)
    for check in checks:
        _validate_provider_check(check)


def _validate_provider_check(check: ProviderCheck) -> None:
    if not check.name:
        raise ValueError("provider check name must be non-empty")
    if not check.tests:
        raise ValueError(f"provider check {check.name} must define at least one live test")
    if not check.test_path:
        raise ValueError(f"provider check {check.name} must define test_path")
    if (check.provider_kind is None) != (check.provider_name is None):
        raise ValueError(
            f"provider check {check.name} must define provider_kind and provider_name together"
        )


def _duplicates(values: Iterable[Any]) -> set[Any]:
    seen: set[Any] = set()
    duplicates: set[Any] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


class CertificationPytestPlugin:
    def __init__(self) -> None:
        self.outcomes: dict[str, Outcome] = {}
        self.reasons: dict[str, str] = {}

    def pytest_runtest_logreport(self, report) -> None:  # pragma: no cover - pytest hook
        if report.when not in {"setup", "call"}:
            return
        if report.outcome == "skipped":
            self.outcomes[report.nodeid] = "skipped"
            self.reasons[report.nodeid] = format_pytest_reason(report.longrepr)
            return
        if report.when != "call":
            return
        self.outcomes[report.nodeid] = "passed" if report.passed else "failed"
        if report.failed:
            self.reasons[report.nodeid] = format_pytest_reason(report.longrepr)


def format_pytest_reason(longrepr: object) -> str:
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        return str(longrepr[2])
    return str(longrepr)


def evaluate_provider_certification(
    outcomes: dict[str, Outcome],
    checks: tuple[ProviderCheck, ...] = DEFAULT_PROVIDER_CHECKS,
    *,
    reasons: dict[str, str] | None = None,
) -> list[ProviderCertificationResult]:
    reason_map = reasons or {}
    return [_evaluate_provider_check(check, outcomes, reason_map) for check in checks]


def _evaluate_provider_check(
    check: ProviderCheck,
    outcomes: dict[str, Outcome],
    reasons: dict[str, str],
) -> ProviderCertificationResult:
    matched = _matched_provider_tests(check, outcomes)
    if not matched:
        return _provider_certification_result(
            check,
            "missing",
            ("no matching test result was collected",),
        )

    failed = _matched_details(matched, reasons, outcome="failed")
    if failed:
        return _provider_certification_result(check, "failed", tuple(failed))

    skipped = _matched_details(matched, reasons, outcome="skipped")
    if skipped:
        return _provider_certification_result(check, "skipped", tuple(skipped))

    passed = [(test_name, nodeid) for test_name, nodeid, outcome in matched if outcome == "passed"]
    passed_test_names = {test_name for test_name, _nodeid in passed}
    if set(check.tests).issubset(passed_test_names):
        return _provider_certification_result(
            check,
            "passed",
            tuple(nodeid for _test_name, nodeid in passed),
        )
    return _provider_certification_result(
        check,
        "missing",
        ("not every required test produced a result",),
    )


def _matched_provider_tests(
    check: ProviderCheck,
    outcomes: dict[str, Outcome],
) -> list[tuple[str, str, Outcome]]:
    return [
        (test_name, nodeid, outcome)
        for nodeid, outcome in outcomes.items()
        for test_name in check.tests
        if _nodeid_matches_test(nodeid, test_name)
    ]


def _matched_details(
    matched: list[tuple[str, str, Outcome]],
    reasons: dict[str, str],
    *,
    outcome: Outcome,
) -> list[str]:
    return [
        _detail_with_reason(nodeid, reasons)
        for _test_name, nodeid, matched_outcome in matched
        if matched_outcome == outcome
    ]


def _provider_certification_result(
    check: ProviderCheck,
    outcome: Outcome,
    details: tuple[str, ...],
) -> ProviderCertificationResult:
    return ProviderCertificationResult(
        name=check.name,
        outcome=outcome,
        tests=check.tests,
        details=details,
        test_path=check.test_path,
        **_result_requirements(check),
    )


def _result_requirements(check: ProviderCheck) -> dict[str, tuple[str, ...]]:
    return {
        "required_env": check.required_env,
        "optional_env": check.optional_env,
        "required_packages": check.required_packages,
    }


def _detail_with_reason(nodeid: str, reasons: dict[str, str]) -> str:
    reason = reasons.get(nodeid)
    if not reason:
        return nodeid
    return f"{nodeid}: {reason}"


def _nodeid_matches_test(nodeid: str, test_name: str) -> bool:
    item_name = nodeid.rsplit("::", 1)[-1]
    base_name = item_name.split("[", 1)[0]
    return base_name == test_name


def build_provider_certification_report(
    results: list[ProviderCertificationResult],
    *,
    test_path: str | None = None,
    test_paths: tuple[str, ...] | None = None,
    selected_providers: tuple[str, ...] | None = None,
    generated_at: str | None = None,
    environ: Mapping[str, str] | None = None,
    package_available: Callable[[str], bool] | None = None,
    pytest_exit_code: int | None = None,
) -> dict[str, Any]:
    summary = {
        "total": len(results),
        "passed": sum(1 for result in results if result.outcome == "passed"),
        "failed": sum(1 for result in results if result.outcome == "failed"),
        "skipped": sum(1 for result in results if result.outcome == "skipped"),
        "missing": sum(1 for result in results if result.outcome == "missing"),
    }
    selected = selected_providers or tuple(result.name for result in results)
    env = environ if environ is not None else os.environ
    is_package_available = package_available or _package_available
    providers = [_provider_report_item(result, env, is_package_available) for result in results]
    requirements_satisfied = all(
        not provider["requirements"]["missing_required_env"]
        and not provider["requirements"]["missing_required_packages"]
        for provider in providers
    )
    pytest_success = pytest_exit_code is None or pytest_exit_code == 0
    result_test_paths = test_paths or tuple(dict.fromkeys(result.test_path for result in results))
    report = {
        "certified": (
            summary["passed"] == summary["total"] and requirements_satisfied and pytest_success
        ),
        "generated_at": generated_at or _utc_timestamp(),
        "test_path": test_path or (result_test_paths[0] if len(result_test_paths) == 1 else None),
        "test_paths": list(result_test_paths),
        "selected_providers": list(selected),
        "summary": summary,
        "providers": providers,
    }
    if pytest_exit_code is not None:
        report["pytest_exit_code"] = pytest_exit_code
        report["pytest_success"] = pytest_success
    return report


def _provider_report_item(
    result: ProviderCertificationResult,
    environ: Mapping[str, str],
    package_available: Callable[[str], bool],
) -> dict[str, Any]:
    return {
        "name": result.name,
        "outcome": result.outcome,
        "test_path": result.test_path,
        "tests": list(result.tests),
        "details": list(result.details),
        "requirements": _requirements_report(
            required_env=result.required_env,
            optional_env=result.optional_env,
            required_packages=result.required_packages,
            environ=environ,
            package_available=package_available,
        ),
    }


def _requirements_report(
    *,
    required_env: tuple[str, ...],
    optional_env: tuple[str, ...],
    required_packages: tuple[str, ...],
    environ: Mapping[str, str],
    package_available: Callable[[str], bool],
) -> dict[str, list[str]]:
    return {
        "required_env": list(required_env),
        "optional_env": list(optional_env),
        "required_packages": list(required_packages),
        "missing_required_env": _missing_required_env(required_env, environ),
        "missing_required_packages": _missing_required_packages(
            required_packages,
            package_available,
        ),
    }


def _missing_required_env(
    required_env: tuple[str, ...],
    environ: Mapping[str, str],
) -> list[str]:
    return [name for name in required_env if not environ.get(name)]


def _missing_required_packages(
    required_packages: tuple[str, ...],
    package_available: Callable[[str], bool],
) -> list[str]:
    return [name for name in required_packages if not package_available(name)]


def _package_available(package_name: str) -> bool:
    import_name = PACKAGE_IMPORT_NAMES.get(package_name, package_name.replace("-", "_"))
    try:
        return importlib.util.find_spec(import_name) is not None
    except ModuleNotFoundError:
        return False


def format_provider_certification_report(
    results: list[ProviderCertificationResult],
    *,
    test_path: str | None = None,
    test_paths: tuple[str, ...] | None = None,
    selected_providers: tuple[str, ...] | None = None,
    generated_at: str | None = None,
    environ: Mapping[str, str] | None = None,
    package_available: Callable[[str], bool] | None = None,
    pytest_exit_code: int | None = None,
) -> str:
    return json.dumps(
        build_provider_certification_report(
            results,
            test_path=test_path,
            test_paths=test_paths,
            selected_providers=selected_providers,
            generated_at=generated_at,
            environ=environ,
            package_available=package_available,
            pytest_exit_code=pytest_exit_code,
        ),
        indent=2,
        sort_keys=True,
    )


def build_provider_preflight_report(
    checks: tuple[ProviderCheck, ...] = DEFAULT_PROVIDER_CHECKS,
    *,
    selected_providers: tuple[str, ...] | None = None,
    generated_at: str | None = None,
    environ: Mapping[str, str] | None = None,
    package_available: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    is_package_available = package_available or _package_available
    providers = []
    for check in checks:
        requirements = _requirements_report(
            required_env=check.required_env,
            optional_env=check.optional_env,
            required_packages=check.required_packages,
            environ=env,
            package_available=is_package_available,
        )
        providers.append(
            {
                "name": check.name,
                "ready": not requirements["missing_required_env"]
                and not requirements["missing_required_packages"],
                "requirements": requirements,
            }
        )
    ready_count = sum(1 for provider in providers if provider["ready"])
    selected = selected_providers or tuple(check.name for check in checks)
    return {
        "ready": ready_count == len(providers),
        "generated_at": generated_at or _utc_timestamp(),
        "selected_providers": list(selected),
        "summary": {
            "total": len(providers),
            "ready": ready_count,
            "blocked": len(providers) - ready_count,
        },
        "providers": providers,
    }


def format_provider_preflight_report(
    checks: tuple[ProviderCheck, ...] = DEFAULT_PROVIDER_CHECKS,
    *,
    selected_providers: tuple[str, ...] | None = None,
    generated_at: str | None = None,
    environ: Mapping[str, str] | None = None,
    package_available: Callable[[str], bool] | None = None,
) -> str:
    return json.dumps(
        build_provider_preflight_report(
            checks,
            selected_providers=selected_providers,
            generated_at=generated_at,
            environ=environ,
            package_available=package_available,
        ),
        indent=2,
        sort_keys=True,
    )


def format_provider_preflight_text(
    checks: tuple[ProviderCheck, ...] = DEFAULT_PROVIDER_CHECKS,
    *,
    environ: Mapping[str, str] | None = None,
    package_available: Callable[[str], bool] | None = None,
) -> str:
    report = build_provider_preflight_report(
        checks,
        environ=environ,
        package_available=package_available,
    )
    lines: list[str] = []
    for provider in report["providers"]:
        status = "ready" if provider["ready"] else "blocked"
        lines.append(f"{provider['name']}: {status}")
        requirements = provider["requirements"]
        _extend_requirement_lines(
            lines,
            "missing env",
            tuple(requirements["missing_required_env"]),
        )
        _extend_requirement_lines(
            lines,
            "missing packages",
            tuple(requirements["missing_required_packages"]),
        )
    return "\n".join(lines)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def expand_provider_check_names(
    provider_names: Iterable[str],
    *,
    checks: tuple[ProviderCheck, ...] | None = None,
) -> tuple[str, ...]:
    catalog = checks or DEFAULT_PROVIDER_CHECKS
    checks_by_name = {check.name: check for check in catalog}
    requested = tuple(provider_names)
    unknown = sorted(set(requested) - set(checks_by_name))
    if unknown:
        raise SystemExit(f"unknown provider check: {', '.join(unknown)}")
    selected: list[str] = []
    seen: set[str] = set()

    def add_provider(name: str) -> None:
        check = checks_by_name[name]
        for dependency in (*PROVIDER_CHECK_DEPENDENCIES.get(name, ()), *check.dependencies):
            add_provider(dependency)
        if name not in seen:
            selected.append(name)
            seen.add(name)

    for provider_name in requested:
        add_provider(provider_name)
    return tuple(selected)


def selected_checks(
    provider_names: list[str],
    *,
    checks: tuple[ProviderCheck, ...] | None = None,
) -> tuple[ProviderCheck, ...]:
    catalog = checks or DEFAULT_PROVIDER_CHECKS
    if not provider_names:
        return catalog
    checks_by_name = {check.name: check for check in catalog}
    return tuple(
        checks_by_name[name] for name in expand_provider_check_names(provider_names, checks=catalog)
    )


def format_provider_checks(
    checks: tuple[ProviderCheck, ...] = DEFAULT_PROVIDER_CHECKS,
    *,
    include_requirements: bool = False,
) -> str:
    lines: list[str] = []
    for check in checks:
        lines.append(f"{check.name}:")
        if include_requirements:
            _extend_requirement_lines(lines, "required env", check.required_env)
            _extend_requirement_lines(lines, "optional env", check.optional_env)
            _extend_requirement_lines(lines, "required packages", check.required_packages)
            lines.append("  tests:")
            lines.extend(f"    - {test_name}" for test_name in check.tests)
        else:
            lines.extend(f"  - {test_name}" for test_name in check.tests)
    return "\n".join(lines)


def format_provider_env_template(
    checks: tuple[ProviderCheck, ...] = DEFAULT_PROVIDER_CHECKS,
) -> str:
    lines = [
        "# fastapi-infra live provider certification environment",
        "# Required values are blank. Optional values are commented out.",
    ]
    seen_env: set[str] = set()
    for check in checks:
        provider_lines: list[str] = []
        for name in check.required_env:
            if name not in seen_env:
                provider_lines.append(f"{name}=")
                seen_env.add(name)
        for name in check.optional_env:
            if name not in seen_env:
                provider_lines.append(f"# {name}=")
                seen_env.add(name)
        if check.required_packages:
            provider_lines.append("# required packages: " + ", ".join(check.required_packages))
        if provider_lines:
            lines.append("")
            lines.append(f"# {check.name}")
            lines.extend(provider_lines)
    return "\n".join(lines)


def _extend_requirement_lines(lines: list[str], title: str, values: tuple[str, ...]) -> None:
    if not values:
        return
    lines.append(f"  {title}:")
    lines.extend(f"    - {value}" for value in values)


def pytest_args_for_checks(test_path: str | None, checks: tuple[ProviderCheck, ...]) -> list[str]:
    test_paths = (
        [test_path]
        if test_path is not None
        else sorted({_resolve_live_provider_test_path(check.test_path) for check in checks})
    )
    test_names = sorted({test_name for check in checks for test_name in check.tests})
    args = [*test_paths, "-q", "-p", "no:cacheprovider"]
    if test_names:
        args.extend(["-k", " or ".join(test_names)])
    return args


def _resolve_live_provider_test_path(test_path: str) -> str:
    if test_path != DEFAULT_LIVE_PROVIDER_TEST_PATH:
        return test_path
    if Path(test_path).exists():
        return test_path
    return PACKAGED_LIVE_PROVIDER_TEST_PATH


def _checks_with_test_path(
    checks: tuple[ProviderCheck, ...],
    test_path: str | None,
) -> tuple[ProviderCheck, ...]:
    if test_path is None:
        return checks
    return tuple(replace(check, test_path=test_path) for check in checks)


def run_pytest_certification(
    test_path: str | None,
    checks: tuple[ProviderCheck, ...],
    *,
    json_output: bool = False,
    environ: Mapping[str, str] | None = None,
) -> int:
    try:
        import pytest
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit("pytest is required to run provider certification") from exc

    effective_checks = _checks_with_test_path(checks, test_path)
    if not effective_checks:
        if json_output:
            print(
                format_provider_certification_report(
                    [],
                    test_path=test_path,
                    selected_providers=(),
                    environ=environ,
                    pytest_exit_code=0,
                )
            )
        return 0

    plugin = CertificationPytestPlugin()
    with _patched_environ(environ):
        if json_output:
            with contextlib.redirect_stdout(sys.stderr):
                pytest_code = pytest.main(
                    pytest_args_for_checks(test_path, effective_checks),
                    plugins=[plugin],
                )
        else:
            pytest_code = pytest.main(
                pytest_args_for_checks(test_path, effective_checks),
                plugins=[plugin],
            )
    results = evaluate_provider_certification(
        plugin.outcomes,
        effective_checks,
        reasons=plugin.reasons,
    )

    if json_output:
        print(
            format_provider_certification_report(
                results,
                test_path=test_path,
                selected_providers=tuple(check.name for check in effective_checks),
                environ=environ,
                pytest_exit_code=int(pytest_code),
            )
        )
    else:
        for result in results:
            detail = "; ".join(result.details)
            print(f"{result.name}: {result.outcome} ({detail})")

    if pytest_code not in {0, 5}:
        return int(pytest_code)
    return 0 if all(result.outcome == "passed" for result in results) else 1


def run_provider_preflight(
    checks: tuple[ProviderCheck, ...],
    *,
    json_output: bool = False,
    environ: Mapping[str, str] | None = None,
) -> int:
    if json_output:
        report = build_provider_preflight_report(
            checks,
            selected_providers=tuple(check.name for check in checks),
            environ=environ,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        report = build_provider_preflight_report(checks, environ=environ)
        print(format_provider_preflight_text(checks, environ=environ))
    return 0 if report["ready"] else 1


@contextmanager
def _patched_environ(environ: Mapping[str, str] | None):
    if environ is None:
        yield
        return
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run opt-in live provider tests and fail unless selected providers "
            "actually pass. Skipped tests are not certification."
        )
    )
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help="Provider check to require. Defaults to every known provider.",
    )
    parser.add_argument(
        "--test-path",
        help=(
            "Pytest path containing live provider tests. Defaults to each provider "
            "check's declared test_path."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable certification report.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List known provider checks and their required live tests.",
    )
    parser.add_argument(
        "--env-template",
        action="store_true",
        help="Print a .env template for selected live provider checks.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Load provider certification environment variables from a .env file.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check required environment variables and packages without running live tests.",
    )
    parser.add_argument(
        "--requirements",
        action="store_true",
        help="Include required env vars, optional env vars, and packages in --list output.",
    )
    args = parser.parse_args(argv)
    provider_checks = get_provider_checks()
    checks = selected_checks(args.provider, checks=provider_checks)
    if args.env_template:
        print(format_provider_env_template(checks))
        return 0
    if args.list:
        print(format_provider_checks(checks, include_requirements=args.requirements))
        return 0
    try:
        environ = load_env_file(args.env_file) if args.env_file is not None else None
    except ValueError as exc:
        parser.error(str(exc))
    if args.preflight:
        return run_provider_preflight(checks, json_output=args.json, environ=environ)
    return run_pytest_certification(
        args.test_path,
        checks,
        json_output=args.json,
        environ=environ,
    )


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
