# Release Readiness Checklist

This project is ready to publish only when every item below has concrete code,
tests, and documentation. Passing the test suite is required, but it is not
enough by itself.

## Current Status

The core package, plugin contracts, local tests, packaging checks, and provider
certification tooling are in place. External providers are adapter-level
verified and live-test-ready, but they are not production-certified until
`fastapi-infra release-check --settings infra.toml` passes,
`fastapi-infra certify-providers --preflight --json` reports every selected
provider as ready, and `fastapi-infra certify-providers --json` reports every
selected provider as passed. The final release gate should pass the
certification JSON back into
`fastapi-infra release-check --provider-certification-report`; release-check
requires certification evidence by default so configured external providers
cannot ship without live verification.

Refresh the live-provider status before every release candidate instead of
treating old local output as current truth:

```bash
fastapi-infra certify-providers --settings infra.production.example.toml \
  --settings-env-file .env \
  --list --requirements
fastapi-infra certify-providers --settings infra.production.example.toml \
  --settings-env-file .env \
  --preflight --env-file provider.env --json
RUN_LIVE_CERTIFICATION=1 scripts/verify-release.sh .env provider.env
```

Missing provider credentials, live backend URLs, or provider SDK packages are
external readiness blockers, not passing release gates.

## Package and Developer Experience

- `fastapi-infra` installs from a wheel without importing optional providers.
- `fastapi-infra new` creates a minimal FastAPI project using only
  `InfraSettings` and `setup_infra`.
- Generated projects enable only the plugins requested by the user.
- Unknown plugin names fail fast with a clear error.
- CI builds the package, checks the distribution metadata, and runs the tests on
  supported Python versions.
- CI installs the built wheel, generates `minimal`, `api`, and `saas` scaffold
  profiles, and runs each generated project's local config check,
  `project-check`, generated tests, production config check, and static release
  gate.
- Generated projects include their own GitHub Actions workflow with the same
  local config, `project-check`, pytest, production config, and static
  release-check gates.
- CI runs `python scripts/verify_local.py`, the same local verification entry
  point documented in the README. That gate runs formatting, type checking, and
  the full test suite; its type-check step covers `infra`, `scripts`, and
  `tests`, so public plugin, provider, runtime, release, smoke-test, and test
  boundaries remain statically checked.
- `python scripts/verify_local.py --package --smoke` builds package artifacts
  in an isolated temporary directory so stale local `dist/` files cannot be
  certified accidentally. Use `--dist-dir <empty-dir>` only when a release run
  needs to preserve the verified wheel and source distribution artifacts.
- `fastapi-infra release-check` fails production configurations that still use
  mock/local/noop/memory providers or skip external provider health probes.
- `fastapi-infra release-check` blocks auth configs that rely on weak JWT
  secrets, placeholder signing keys, or invalid/under-strength API key hashes.
- `fastapi-infra release-check` also blocks cache without Redis, payment
  database stores without MySQL, and webhook configs without durable storage,
  signed provider config, and declared `required_providers` coverage.
- Webhook notifications must declare a signing secret, health URL, and
  `health_probe=true` before they are accepted for production.
- `fastapi-infra release-check --migrations migrations` validates that enabled
  plugins with schema contracts have corresponding SQL migration files.
- `fastapi-infra release-check` blocks memory-backed rate limiting, accepts the
  Redis rate-limit backend only with Redis backing, and warns when observability
  is still using memory metrics or disabled tracing.
- `fastapi-infra release-check` fails external provider configs that do not have
  a certification report by default; `--static-only` is only for local static scans.
- Provider certification reports must cover every known real provider declared
  in configuration, not only each plugin's `default_provider`.
- Provider certification reports used for configured real providers must have a
  parseable `generated_at` timestamp, are rejected when generated in the future,
  and are treated as stale after 24 hours.
- Release checks compare provider certification reports against the current
  certification catalog, including third-party provider check entry points,
  required live test names, test paths, and
  required environment/package metadata.
- Provider certification summary counts must match the provider result entries,
  and duplicate or malformed provider results are blocked.
- Provider certification `selected_providers` must be known provider
  checks, must be unique, and must match the provider result names exactly.
- Provider result evidence must come from each provider check's declared live
  test path, and unmet requirement fields must be typed as lists so malformed
  reports cannot imply that missing env vars or packages are empty.
- Production release checks require provider certification reports to cover the
  declared test path for each selected provider check.
- Third-party provider entry points can participate in production release when
  they also expose `fastapi_infra.provider_checks` certification metadata and
  the report includes passing evidence for those checks.
- Live provider certification can emit a JSON report for CI artifacts and
  release evidence.
- Provider certification reports include pytest process success evidence, and a
  pytest collection/import/internal error cannot produce a certified report even
  if collected provider outcomes were marked passed.

## Plugin Contract

- Every plugin can be enabled and disabled through `InfraSettings`.
- Optional integrations fail with explicit configuration or dependency errors;
  they do not silently fall back to mock providers.
- Plugin and provider configuration rejects unknown fields so misspelled
  production settings fail during startup instead of being ignored.
- Plugin startup failure rolls back already-started resources.
- Plugin shutdown failure preserves enough state for a retry.
- Health checks report real local state and never return fabricated upstream
  success.
- External providers without an upstream probe report `DEGRADED`, not
  `HEALTHY`, until live certification or a real provider-specific check proves
  reachability.

## Real Providers

- AI supports OpenAI, Anthropic, and Gemini through their official SDK boundary.
- Payment includes at least one real provider with checkout creation, checkout
  lookup, webhook signature verification, and explicit error handling.
- Storage includes at least one real object-store provider with authenticated
  PUT, GET, DELETE, and existence checks.
- Speech includes at least one real ASR/TTS provider before claiming production
  speech support.
- Observability can expose real Prometheus metrics and OpenTelemetry spans
  through standard integrations.

## Production Safety

- Authentication has documented token, JWT signing-key rotation, API key, scope,
  and role semantics.
- Secrets are never logged or exposed in health responses.
- Payment provider results can be durably recorded without coupling the
  infrastructure layer to an application order model.
- Background task adapters expose retry and failure behavior explicitly. Redis
  task adapters require a Redis backing and Redis provider certification
  evidence in release checks.
- Webhook idempotency can be backed by durable storage; in-memory stores are
  documented as local-development only.
- HTTP, database, cache, task, and provider clients are closed during shutdown.
- Integration tests cover configured external backends, or the feature remains
  documented as adapter-level verified rather than production-certified.
- A skipped provider certification check is recorded as skipped in the release
  report and cannot be counted as production-ready.

## Documentation

- README shows the small public API and the CLI quickstart.
- Plugin docs explain enabled/disabled behavior and provider configuration.
- Provider docs distinguish local/mock development providers from real providers.
- Release notes list breaking changes without compatibility shims.
