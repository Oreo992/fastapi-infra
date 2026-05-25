import json
import tomllib
from pathlib import Path

import pytest

from infra.provider_certification import (
    DEFAULT_LIVE_PROVIDER_TEST_PATH,
    PACKAGED_LIVE_PROVIDER_TEST_PATH,
    ProviderCheck,
    build_provider_certification_report,
    build_provider_preflight_report,
    evaluate_provider_certification,
    expand_provider_check_names,
    format_provider_certification_report,
    format_provider_checks,
    format_provider_env_template,
    format_provider_preflight_report,
    format_provider_preflight_text,
    format_pytest_reason,
    get_provider_checks,
    pytest_args_for_checks,
    run_pytest_certification,
    selected_checks,
)


class FakeProviderCheckEntryPoint:
    def __init__(self, name: str, loaded: object) -> None:
        self.name = name
        self.loaded = loaded

    def load(self) -> object:
        return self.loaded


def test_provider_certification_passes_only_when_required_live_tests_pass():
    checks = (ProviderCheck("stripe", ("test_live_stripe_checkout_session_creation",)),)

    results = evaluate_provider_certification(
        {
            "tests/integration/test_live_providers.py::test_live_stripe_checkout_session_creation": "passed"
        },
        checks,
    )

    assert results[0].name == "stripe"
    assert results[0].outcome == "passed"


def test_provider_certification_report_requires_passed_tests_env_and_packages():
    checks = (
        ProviderCheck(
            "stripe",
            ("test_live_stripe_checkout_session_creation",),
            required_env=("STRIPE_API_KEY",),
            required_packages=("stripe-sdk",),
        ),
    )
    results = evaluate_provider_certification(
        {
            "tests/integration/test_live_providers.py::test_live_stripe_checkout_session_creation": "passed"
        },
        checks,
    )

    blocked_report = build_provider_certification_report(
        results,
        environ={},
        package_available=lambda package: False,
    )
    ready_report = build_provider_certification_report(
        results,
        environ={"STRIPE_API_KEY": "sk-test"},
        package_available=lambda package: True,
    )

    assert blocked_report["summary"]["passed"] == 1
    assert blocked_report["certified"] is False
    assert blocked_report["providers"][0]["requirements"]["missing_required_env"] == [
        "STRIPE_API_KEY"
    ]
    assert blocked_report["providers"][0]["requirements"]["missing_required_packages"] == [
        "stripe-sdk"
    ]
    assert ready_report["certified"] is True


def test_provider_certification_treats_skips_as_not_certified():
    checks = (ProviderCheck("stripe", ("test_live_stripe_checkout_session_creation",)),)
    nodeid = (
        "tests/integration/test_live_providers.py::" "test_live_stripe_checkout_session_creation"
    )

    results = evaluate_provider_certification(
        {nodeid: "skipped"},
        checks,
        reasons={nodeid: "live provider test requires env vars: STRIPE_API_KEY"},
    )

    assert results[0].outcome == "skipped"
    assert results[0].details == (
        f"{nodeid}: live provider test requires env vars: STRIPE_API_KEY",
    )


def test_provider_certification_reports_missing_required_tests():
    checks = (ProviderCheck("stripe", ("test_live_stripe_checkout_session_creation",)),)

    results = evaluate_provider_certification({}, checks)

    assert results[0].outcome == "missing"
    assert results[0].details == ("no matching test result was collected",)


def test_provider_certification_matches_exact_pytest_test_name():
    checks = (ProviderCheck("stripe", ("test_live_stripe_checkout_session_creation",)),)

    results = evaluate_provider_certification(
        {
            (
                "tests/integration/test_live_providers.py::"
                "test_live_stripe_checkout_session_creation_extra"
            ): "passed"
        },
        checks,
    )

    assert results[0].outcome == "missing"


def test_provider_certification_matches_parametrized_pytest_nodeid():
    checks = (ProviderCheck("stripe", ("test_live_stripe_checkout_session_creation",)),)

    results = evaluate_provider_certification(
        {
            (
                "tests/integration/test_live_providers.py::"
                "test_live_stripe_checkout_session_creation[live]"
            ): "passed"
        },
        checks,
    )

    assert results[0].outcome == "passed"


def test_provider_certification_accepts_multiple_passed_cases_for_same_test():
    checks = (ProviderCheck("stripe", ("test_live_stripe_checkout_session_creation",)),)

    results = evaluate_provider_certification(
        {
            (
                "tests/integration/test_live_providers.py::"
                "test_live_stripe_checkout_session_creation[case-a]"
            ): "passed",
            (
                "tests/integration/test_live_providers.py::"
                "test_live_stripe_checkout_session_creation[case-b]"
            ): "passed",
        },
        checks,
    )

    assert results[0].outcome == "passed"
    assert results[0].details == (
        "tests/integration/test_live_providers.py::"
        "test_live_stripe_checkout_session_creation[case-a]",
        "tests/integration/test_live_providers.py::"
        "test_live_stripe_checkout_session_creation[case-b]",
    )


def test_provider_certification_still_requires_every_named_test():
    checks = (
        ProviderCheck(
            "stripe",
            (
                "test_live_stripe_checkout_session_creation",
                "test_live_stripe_webhook_signature_entrypoint",
            ),
        ),
    )

    results = evaluate_provider_certification(
        {
            (
                "tests/integration/test_live_providers.py::"
                "test_live_stripe_checkout_session_creation[case-a]"
            ): "passed",
            (
                "tests/integration/test_live_providers.py::"
                "test_live_stripe_checkout_session_creation[case-b]"
            ): "passed",
        },
        checks,
    )

    assert results[0].outcome == "missing"
    assert results[0].details == ("not every required test produced a result",)


def test_format_pytest_reason_removes_location_tuple_noise():
    reason = format_pytest_reason(
        (
            "/Users/example/project/tests/integration/test_live_providers.py",
            48,
            "Skipped: live provider test requires env vars: STRIPE_API_KEY",
        )
    )

    assert reason == "Skipped: live provider test requires env vars: STRIPE_API_KEY"


def test_provider_certification_report_is_machine_readable():
    checks = (
        ProviderCheck(
            "stripe",
            ("test_live_stripe_checkout_session_creation",),
            required_env=("STRIPE_API_KEY",),
            optional_env=("STRIPE_API_BASE",),
            required_packages=("stripe-sdk",),
        ),
        ProviderCheck("s3", ("test_live_s3_put_get_list_and_presign",)),
    )
    results = evaluate_provider_certification(
        {
            "tests/integration/test_live_providers.py::test_live_stripe_checkout_session_creation": "passed",
            "tests/integration/test_live_providers.py::test_live_s3_put_get_list_and_presign": "skipped",
        },
        checks,
    )

    report = build_provider_certification_report(
        results,
        test_path="tests/integration/test_live_providers.py",
        selected_providers=("stripe", "s3"),
        generated_at="2026-05-12T00:00:00Z",
        environ={"STRIPE_API_KEY": "sk-test"},
        package_available=lambda package: package != "stripe-sdk",
    )
    encoded = format_provider_certification_report(
        results,
        test_path="tests/integration/test_live_providers.py",
        selected_providers=("stripe", "s3"),
        generated_at="2026-05-12T00:00:00Z",
    )

    assert report["certified"] is False
    assert report["generated_at"] == "2026-05-12T00:00:00Z"
    assert report["test_path"] == "tests/integration/test_live_providers.py"
    assert report["selected_providers"] == ["stripe", "s3"]
    assert report["summary"] == {
        "total": 2,
        "passed": 1,
        "failed": 0,
        "skipped": 1,
        "missing": 0,
    }
    assert report["providers"][0]["name"] == "stripe"
    assert report["providers"][0]["outcome"] == "passed"
    assert report["providers"][0]["requirements"] == {
        "required_env": ["STRIPE_API_KEY"],
        "optional_env": ["STRIPE_API_BASE"],
        "required_packages": ["stripe-sdk"],
        "missing_required_env": [],
        "missing_required_packages": ["stripe-sdk"],
    }
    assert report["providers"][1]["requirements"]["missing_required_env"] == []
    assert '"certified": false' in encoded
    assert '"name": "s3"' in encoded


def test_provider_certification_report_marks_pytest_internal_errors_uncertified(
    capsys,
    monkeypatch,
):
    import pytest as pytest_module

    def fake_pytest_main(args, plugins):
        plugins[0].outcomes[
            "tests/integration/test_live_providers.py::test_live_custom_provider"
        ] = "passed"
        return 2

    monkeypatch.setattr(pytest_module, "main", fake_pytest_main)

    result = run_pytest_certification(
        "tests/integration/test_live_providers.py",
        (ProviderCheck("custom", ("test_live_custom_provider",)),),
        json_output=True,
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 2
    assert report["certified"] is False
    assert report["pytest_exit_code"] == 2
    assert report["pytest_success"] is False
    assert report["summary"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "missing": 0,
    }


def test_provider_certification_report_lists_missing_required_env():
    checks = (
        ProviderCheck(
            "stripe",
            ("test_live_stripe_checkout_session_creation",),
            required_env=("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET"),
        ),
    )
    results = evaluate_provider_certification({}, checks)

    report = build_provider_certification_report(results, environ={"STRIPE_API_KEY": "sk-test"})

    assert report["providers"][0]["requirements"]["missing_required_env"] == [
        "STRIPE_WEBHOOK_SECRET"
    ]


def test_provider_certification_report_lists_missing_required_packages():
    checks = (
        ProviderCheck(
            "gemini-ai",
            ("test_live_gemini_chat_and_embedding",),
            required_packages=("google-genai",),
        ),
    )
    results = evaluate_provider_certification({}, checks)

    report = build_provider_certification_report(
        results,
        package_available=lambda package: False,
    )

    assert report["providers"][0]["requirements"]["missing_required_packages"] == ["google-genai"]


def test_provider_preflight_report_checks_env_and_required_packages():
    checks = (
        ProviderCheck(
            "gemini-ai",
            ("test_live_gemini_chat_and_embedding",),
            required_env=("GEMINI_API_KEY", "GEMINI_LIVE_CHAT_MODEL"),
            required_packages=("google-genai",),
        ),
    )

    report = build_provider_preflight_report(
        checks,
        selected_providers=("gemini-ai",),
        generated_at="2026-05-12T00:00:00Z",
        environ={"GEMINI_API_KEY": "key"},
        package_available=lambda package: False,
    )
    encoded = format_provider_preflight_report(
        checks,
        selected_providers=("gemini-ai",),
        generated_at="2026-05-12T00:00:00Z",
        environ={"GEMINI_API_KEY": "key"},
        package_available=lambda package: False,
    )
    text = format_provider_preflight_text(
        checks,
        environ={"GEMINI_API_KEY": "key"},
        package_available=lambda package: False,
    )

    assert report["ready"] is False
    assert report["summary"] == {"total": 1, "ready": 0, "blocked": 1}
    assert report["providers"][0]["requirements"]["missing_required_env"] == [
        "GEMINI_LIVE_CHAT_MODEL"
    ]
    assert report["providers"][0]["requirements"]["missing_required_packages"] == ["google-genai"]
    assert '"ready": false' in encoded
    assert "gemini-ai: blocked" in text
    assert "    - GEMINI_LIVE_CHAT_MODEL" in text
    assert "    - google-genai" in text


def test_provider_preflight_report_is_ready_when_requirements_exist():
    checks = (
        ProviderCheck(
            "redis",
            ("test_live_redis_cache_service_round_trip",),
            required_env=("REDIS_LIVE_URL",),
            required_packages=("redis",),
        ),
    )

    report = build_provider_preflight_report(
        checks,
        environ={"REDIS_LIVE_URL": "redis://localhost:6379/0"},
        package_available=lambda package: True,
    )

    assert report["ready"] is True
    assert report["summary"] == {"total": 1, "ready": 1, "blocked": 0}


def test_provider_preflight_report_is_ready_when_no_checks_are_required():
    report = build_provider_preflight_report(())

    assert report["ready"] is True
    assert report["summary"] == {"total": 0, "ready": 0, "blocked": 0}
    assert report["providers"] == []


def test_selected_checks_rejects_unknown_provider_name():
    with pytest.raises(SystemExit, match="unknown provider check"):
        selected_checks(["not-a-provider"])


def test_provider_check_catalog_loads_external_entry_points():
    external_check = ProviderCheck(
        "acme-ai",
        ("test_live_acme_chat",),
        required_env=("ACME_API_KEY",),
        required_packages=("acme-sdk",),
        test_path="tests/integration/test_acme_live.py",
        provider_kind="ai",
        provider_name="acme",
    )

    checks = get_provider_checks(
        entry_points_loader=lambda group: [
            FakeProviderCheckEntryPoint("acme", lambda: external_check)
        ]
    )
    selected = selected_checks(["acme-ai"], checks=checks)

    assert selected == (external_check,)
    assert pytest_args_for_checks(None, selected) == [
        "tests/integration/test_acme_live.py",
        "-q",
        "-p",
        "no:cacheprovider",
        "-k",
        "test_live_acme_chat",
    ]


def test_provider_check_catalog_rejects_duplicate_provider_identity():
    duplicate_openai = ProviderCheck(
        "custom-openai-ai",
        ("test_live_custom_openai",),
        provider_kind="ai",
        provider_name="openai",
    )

    with pytest.raises(ValueError, match="duplicate provider certification identity: ai:openai"):
        get_provider_checks(
            entry_points_loader=lambda group: [
                FakeProviderCheckEntryPoint("duplicate", duplicate_openai)
            ]
        )


def test_provider_check_catalog_rejects_invalid_entry_point_values():
    with pytest.raises(ValueError, match="must load ProviderCheck"):
        get_provider_checks(
            entry_points_loader=lambda group: [FakeProviderCheckEntryPoint("invalid", object())]
        )


def test_format_provider_checks_lists_required_live_tests():
    output = format_provider_checks(
        (
            ProviderCheck(
                "stripe",
                (
                    "test_live_stripe_checkout_session_creation",
                    "test_live_stripe_webhook_signature_entrypoint",
                ),
            ),
        )
    )

    assert output == (
        "stripe:\n"
        "  - test_live_stripe_checkout_session_creation\n"
        "  - test_live_stripe_webhook_signature_entrypoint"
    )


def test_format_provider_checks_can_include_environment_requirements():
    output = format_provider_checks(
        (
            ProviderCheck(
                "stripe",
                ("test_live_stripe_checkout_session_creation",),
                required_env=("STRIPE_API_KEY",),
                optional_env=("STRIPE_API_BASE", "STRIPE_LIVE_TIMEOUT"),
            ),
        ),
        include_requirements=True,
    )

    assert output == (
        "stripe:\n"
        "  required env:\n"
        "    - STRIPE_API_KEY\n"
        "  optional env:\n"
        "    - STRIPE_API_BASE\n"
        "    - STRIPE_LIVE_TIMEOUT\n"
        "  tests:\n"
        "    - test_live_stripe_checkout_session_creation"
    )


def test_format_provider_env_template_lists_required_and_commented_optional_values():
    output = format_provider_env_template(
        (
            ProviderCheck(
                "openai-ai",
                ("test_live_openai_chat_and_embedding",),
                required_env=("OPENAI_API_KEY", "OPENAI_LIVE_CHAT_MODEL"),
                optional_env=("OPENAI_API_BASE",),
                required_packages=("openai",),
            ),
            ProviderCheck(
                "openai-speech",
                ("test_live_openai_speech_transcription",),
                required_env=("OPENAI_API_KEY",),
                optional_env=("OPENAI_VOICE",),
            ),
        )
    )

    assert output == (
        "# fastapi-infra live provider certification environment\n"
        "# Required values are blank. Optional values are commented out.\n"
        "\n"
        "# openai-ai\n"
        "OPENAI_API_KEY=\n"
        "OPENAI_LIVE_CHAT_MODEL=\n"
        "# OPENAI_API_BASE=\n"
        "# required packages: openai\n"
        "\n"
        "# openai-speech\n"
        "# OPENAI_VOICE="
    )
    assert output.count("OPENAI_API_KEY=") == 1


def test_stripe_provider_check_includes_mysql_store_dependency():
    checks = selected_checks(["stripe"])

    assert [check.name for check in checks] == ["mysql", "stripe"]
    assert checks[1].tests == (
        "test_live_stripe_checkout_session_creation",
        "test_live_stripe_checkout_persists_to_mysql_store",
        "test_live_stripe_webhook_signature_entrypoint",
    )
    assert checks[1].required_env == ("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET")
    assert "STRIPE_LIVE_TIMEOUT" in checks[1].optional_env


def test_selected_checks_deduplicates_provider_dependencies():
    checks = selected_checks(["mysql", "stripe"])

    assert [check.name for check in checks] == ["mysql", "stripe"]


def test_expand_provider_check_names_can_be_shared_by_release_gates():
    assert expand_provider_check_names(["stripe"]) == ("mysql", "stripe")


def test_openai_speech_provider_check_requires_asr_and_tts_live_tests():
    checks = selected_checks(["openai-speech"])

    assert checks[0].name == "openai-speech"
    assert checks[0].tests == (
        "test_live_openai_speech_transcription",
        "test_live_openai_speech_synthesis",
    )
    assert checks[0].required_env == ("OPENAI_API_KEY",)
    assert "OPENAI_SPEECH_TIMEOUT" in checks[0].optional_env


def test_pytest_args_for_checks_filters_to_selected_live_tests():
    checks = (
        ProviderCheck("stripe", ("test_live_stripe_checkout_session_creation",)),
        ProviderCheck("s3", ("test_live_s3_put_get_list_and_presign",)),
    )

    args = pytest_args_for_checks("tests/integration/test_live_providers.py", checks)

    assert args == [
        "tests/integration/test_live_providers.py",
        "-q",
        "-p",
        "no:cacheprovider",
        "-k",
        "test_live_s3_put_get_list_and_presign or test_live_stripe_checkout_session_creation",
    ]


def test_pytest_args_for_default_checks_falls_back_to_packaged_live_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    checks = (ProviderCheck("redis", ("test_live_redis_cache_service_round_trip",)),)

    args = pytest_args_for_checks(None, checks)

    assert args == [
        PACKAGED_LIVE_PROVIDER_TEST_PATH,
        "-q",
        "-p",
        "no:cacheprovider",
        "-k",
        "test_live_redis_cache_service_round_trip",
    ]
    assert DEFAULT_LIVE_PROVIDER_TEST_PATH not in args


def test_provider_certification_module_list_honors_selected_provider(capsys):
    from infra import provider_certification

    result = provider_certification.main(["--provider", "stripe", "--list"])

    captured = capsys.readouterr()
    assert result == 0
    assert "stripe:" in captured.out
    assert "test_live_stripe_checkout_session_creation" in captured.out
    assert "s3:" not in captured.out
    assert captured.err == ""


def test_provider_certification_module_preflight_honors_selected_provider(
    capsys,
    monkeypatch,
):
    from infra import provider_certification

    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)

    result = provider_certification.main(["--provider", "stripe", "--preflight", "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert report["ready"] is False
    assert report["selected_providers"] == ["mysql", "stripe"]
    assert report["summary"] == {"total": 2, "ready": 0, "blocked": 2}
    assert report["providers"][1]["requirements"]["missing_required_env"] == [
        "STRIPE_API_KEY",
        "STRIPE_WEBHOOK_SECRET",
    ]
    assert captured.err == ""


def test_provider_certification_module_preflight_can_load_env_file(
    tmp_path,
    capsys,
    monkeypatch,
):
    from infra import provider_certification

    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(provider_certification, "_package_available", lambda package: True)
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        "\n".join(
            [
                "MYSQL_LIVE_HOST=localhost",
                "MYSQL_LIVE_USER=user",
                "MYSQL_LIVE_PASSWORD=password",
                "MYSQL_LIVE_DB=app",
                "STRIPE_API_KEY=sk-file",
                "STRIPE_WEBHOOK_SECRET=whsec-file",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = provider_certification.main(
        [
            "--provider",
            "stripe",
            "--preflight",
            "--json",
            "--env-file",
            str(env_file),
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 0
    assert report["ready"] is True
    assert report["selected_providers"] == ["mysql", "stripe"]
    assert report["providers"][0]["requirements"]["missing_required_env"] == []
    assert report["providers"][1]["requirements"]["missing_required_env"] == []
    assert captured.err == ""


def test_live_provider_extra_includes_every_sdk_backed_certification_dependency():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    live_providers = set(pyproject["project"]["optional-dependencies"]["live-providers"])

    assert "aiomysql>=0.2.0,<0.3.0" in live_providers
    assert "redis>=6.4.0,<7.0.0" in live_providers
    assert "openai>=1.0.0" in live_providers
    assert "anthropic>=0.40.0" in live_providers
    assert "google-genai>=1.0.0" in live_providers
