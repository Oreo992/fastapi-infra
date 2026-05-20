# Provider Certification

This document tracks which external integrations are adapter-tested, which have
opt-in live tests, and what evidence is still missing before a production
certification claim.

## Certification Levels

- `unit`: deterministic tests cover request mapping, errors, and local state.
- `live-test-ready`: opt-in integration tests exist and skip without credentials.
- `certified`: live tests have passed against real provider credentials in CI or
  a recorded release run.

## Current Matrix

| Capability | Provider | Unit Evidence | Live Test | Status |
| --- | --- | --- | --- | --- |
| AI chat | OpenAI | `tests/plugins/test_ai_sdk_adapters.py` | `test_live_openai_chat_and_embedding` | live-test-ready |
| AI embeddings | OpenAI | `tests/plugins/test_ai_sdk_adapters.py` | `test_live_openai_chat_and_embedding` | live-test-ready |
| AI chat | Anthropic | `tests/plugins/test_ai_sdk_adapters.py` | `test_live_anthropic_chat` | live-test-ready |
| AI chat | Gemini | `tests/plugins/test_ai_sdk_adapters.py` | `test_live_gemini_chat_and_embedding` | live-test-ready |
| AI embeddings | Gemini | `tests/plugins/test_ai_sdk_adapters.py` | `test_live_gemini_chat_and_embedding` | live-test-ready |
| Speech ASR | OpenAI | `tests/plugins/test_speech_openai_provider.py` | `test_live_openai_speech_transcription` | live-test-ready |
| Speech TTS | OpenAI | `tests/plugins/test_speech_openai_provider.py` | `test_live_openai_speech_synthesis` | live-test-ready |
| Payment checkout + lookup | Stripe | `tests/plugins/test_payment_stripe_provider.py` | `test_live_stripe_checkout_session_creation` | live-test-ready |
| Payment durable store integration | Stripe + MySQL | `tests/plugins/test_payment_plugin.py` | `test_live_stripe_checkout_persists_to_mysql_store` | live-test-ready |
| Payment webhooks | Stripe | `tests/plugins/test_payment_stripe_provider.py` | `test_live_stripe_webhook_signature_entrypoint` | live-test-ready |
| Object storage PUT/GET/LIST/DELETE/exists/presign | S3-compatible | `tests/plugins/test_storage_s3_provider.py` | `test_live_s3_put_get_list_and_presign` | live-test-ready |
| Email | SMTP | `tests/plugins/test_peripheral_plugins.py` | `test_live_smtp_notification_send` | live-test-ready |
| Database | MySQL | `tests/core/test_database_manager_config.py` | `test_live_mysql_database_manager_round_trip` | live-test-ready |
| Cache | Redis | `tests/core/test_database_manager_config.py` | `test_live_redis_cache_service_round_trip` | live-test-ready |

## Release Gate

A release may describe a provider as production-certified only after its live
test has passed with real credentials. A skipped live test means the provider is
adapter-verified, not certified.

Recommended command:

```bash
pip install -e ".[dev,live-providers]"
fastapi-infra certify-providers
```

List the provider groups and the live tests each group requires:

```bash
fastapi-infra certify-providers --list
```

Include the required environment variables, optional environment variables, and
required optional packages for each provider group:

```bash
fastapi-infra certify-providers --list --requirements
```

Combine `--provider` with `--list --requirements` to inspect only one provider
group:

```bash
fastapi-infra certify-providers --provider stripe --list --requirements
```

Generate a `.env`/CI-secrets template for all provider groups, or only one
provider group:

```bash
fastapi-infra certify-providers --env-template
fastapi-infra certify-providers --provider stripe --env-template
```

The template prints required variables as blank assignments, comments optional
variables, lists required packages as comments, and de-duplicates variables that
are shared across providers.

Run a fast preflight before live tests to check required environment variables
and packages without calling any provider:

```bash
fastapi-infra certify-providers --preflight
fastapi-infra certify-providers --provider gemini-ai --preflight --json
fastapi-infra certify-providers --env-file provider.env --preflight --json
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --env-file provider.env --preflight --json
```

Preflight exits non-zero when any selected provider is missing required
environment variables or packages. It is not certification; it only proves the
process is ready to attempt the live tests.

Use `--settings infra.production.example.toml` to select the provider checks
required by the active production config. This follows plugin provider
certification hooks and includes provider dependencies automatically; for
example, a Stripe payment config selects both `mysql` and `stripe`. Use
`--settings-env-file .env` when the settings file contains runtime `$env`
references; keep live provider credentials in `provider.env` and pass them with
`--env-file provider.env`.

Use `--env-file provider.env` when credentials should come from a local
dotenv-style file instead of the current shell environment. The parser supports
`KEY=VALUE`, `export KEY=VALUE`, single-quoted values, double-quoted values,
blank lines, comments, and inline comments after unquoted values. Values from
the file override the current process environment while provider certification
runs.

Use `--provider name` to certify one provider group, for example:

```bash
fastapi-infra certify-providers --provider stripe
```

Provider groups cover the complete production claim for that provider. For
example, `stripe` requires checkout-session creation, webhook signature
verification, and the `PaymentService` + `SqlPaymentStore` + MySQL persistence
path to pass. In the live provider workflow, selecting `stripe` also selects
`mysql`, because the production payment release gate requires durable
database-backed storage for provider results. Stripe certification covers the
signature algorithm and provider path; `release-check` still validates that the
webhooks production config declares `providers.stripe` and `required_providers`,
and `install_webhook_routes` validates that matching providers are installed.
The `s3` check writes an object,
reads it through signed requests, lists it, and fetches it through the generated
presigned URL.
Live tests for SDK/HTTP-backed providers also execute the provider's
`health_check()` probe with the same credentials, so certification verifies both
runtime reachability and the primary provider operation.

Use `--json` when the result needs to be attached to a release or parsed by CI:

```bash
fastapi-infra certify-providers --env-file provider.env --json > provider-certification.json
```

The JSON report contains an overall `certified` boolean, generation timestamp,
test path, selected provider names, per-outcome summary counts, each provider's
test evidence, each provider group's requirements, and `missing_required_env`
and `missing_required_packages` for the current process environment. It is still
a failing release gate unless every selected provider has outcome `passed`.
Reports generated by the built-in runner also include `pytest_exit_code` and
`pytest_success`; a pytest collection, import, or internal error forces
`certified=false` even if individual collected test outcomes looked passed.
When the report is supplied to `fastapi-infra release-check` for a production
configuration with real providers, `generated_at` must be present, parseable as
a timezone-aware timestamp, and no older than 24 hours. Release-check also
validates that each provider result lists the current required live tests and
required environment/package metadata from the active certification catalog.
The report summary must also match the provider result entries exactly.
`selected_providers` must be a unique list of provider names and must match the
provider result names exactly.
For production release checks, the report must include either `test_path` or
`test_paths`, and those paths must cover the test path declared by every
selected provider check.

## Third-Party Provider Checks

Third-party provider packages can extend the certification catalog with the
`fastapi_infra.provider_checks` entry point:

```toml
[project.entry-points."fastapi_infra.provider_checks"]
acme_ai = "acme_ai.certification:provider_checks"
```

The entry point must load either a `ProviderCheck` or an iterable of
`ProviderCheck` objects:

```python
from infra.provider_certification import ProviderCheck


def provider_checks() -> tuple[ProviderCheck, ...]:
    return (
        ProviderCheck(
            name="acme-ai",
            provider_kind="ai",
            provider_name="acme",
            tests=("test_live_acme_chat",),
            test_path="tests/integration/test_acme_live.py",
            required_env=("ACME_API_KEY",),
            required_packages=("acme-sdk",),
        ),
    )
```

`provider_kind` and `provider_name` bind the certification check to a configured
runtime provider. For example, an AI provider entry point named `acme` should
use `provider_kind="ai"` and `provider_name="acme"`. Release-check then treats
that provider as certifiable and requires evidence for the declared check name
instead of blocking it as uncertified. Check names and provider
`kind/name` identities must be unique across the active catalog.

The command runs the live integration tests and fails if a selected provider is
skipped, missing, or failed. Plain `pytest tests/integration -q` is useful while
developing tests, but it is not a release gate because skips still produce a
zero exit code.

If any live provider is intentionally out of scope for a release, document that
decision in the release notes instead of silently implying production support.

The `Live Providers` GitHub Actions workflow exposes the same gate through
`workflow_dispatch`. Configure the provider secrets and model variables there
before using the resulting run as certification evidence. The workflow uploads
`provider-preflight.json`, `provider-certification.json`, `release-check.json`,
and
`provider-env-template.env` as artifacts when available. This lets a release
reviewer distinguish missing configuration, skipped tests, and failed live
provider behavior.
