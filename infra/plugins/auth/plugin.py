from pydantic import BaseModel, ConfigDict, Field

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.auth.models import HashedApiKeyRecord, JwtSigningKeyRecord
from infra.plugins.auth.service import (
    API_KEY_HASH_ALGORITHM,
    API_KEY_HASH_ITERATIONS,
    AuthService,
)
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.release_checks import PluginReleaseIssue, release_error

MIN_JWT_SECRET_LENGTH = 32
WEAK_AUTH_SECRET_VALUES = frozenset(
    {
        "change-me",
        "changeme",
        "dev-secret",
        "dev-secret-change-me",
        "jwt-secret",
        "password",
        "replace-with-32-byte-random-secret",
        "secret",
        "test",
        "test-secret",
    }
)


class AuthPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hashed_api_keys: dict[str, HashedApiKeyRecord] | list[HashedApiKeyRecord] = Field(
        default_factory=dict
    )
    jwt_secret: str | None = Field(default=None, repr=False)
    jwt_signing_keys: dict[str, JwtSigningKeyRecord] | list[JwtSigningKeyRecord] = Field(
        default_factory=dict
    )
    jwt_key_id: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    access_token_ttl_seconds: int = Field(default=3600, gt=0)


class AuthPlugin:
    metadata = PluginMetadata(
        name="auth",
        version="1.0.0",
        default_enabled=False,
        provides=["auth"],
    )
    config_model = AuthPluginConfig
    manifest_hints = {
        "service_keys": {"auth": "infra.plugins.AUTH_SERVICE"},
        "env_vars": ["JWT_SECRET"],
        "local_config_example": {
            "jwt_secret": "dev-secret-change-me",
        },
        "production_config_example": {
            "jwt_secret": "${JWT_SECRET}",
        },
        "release_check_notes": [
            "Production auth must configure API keys or JWT signing.",
        ],
    }

    def release_check(
        self,
        settings: InfraSettings,
        config: AuthPluginConfig,
    ) -> list[PluginReleaseIssue]:
        issues: list[PluginReleaseIssue] = []
        if not config.jwt_secret and not config.jwt_signing_keys and not config.hashed_api_keys:
            return [
                release_error(
                    "credentials_required",
                    "auth is enabled without jwt_secret, jwt_signing_keys, or hashed_api_keys",
                )
            ]

        if config.jwt_secret is not None and _weak_auth_secret(config.jwt_secret):
            issues.append(
                release_error(
                    "weak_jwt_secret",
                    (
                        "production jwt_secret must be at least 32 characters "
                        "and must not use a placeholder"
                    ),
                )
            )
        for key_id, secret in _jwt_signing_secrets(config).items():
            if _weak_auth_secret(secret):
                issues.append(
                    release_error(
                        "weak_jwt_signing_key",
                        (
                            f"JWT signing key {key_id!r} must be at least 32 "
                            "characters and must not use a placeholder"
                        ),
                    )
                )
        for key_id, encoded_hash in _hashed_api_key_hashes(config).items():
            hash_issue = _api_key_hash_issue(encoded_hash)
            if hash_issue is not None:
                issues.append(
                    release_error(
                        hash_issue,
                        (
                            f"API key hash {key_id!r} must use "
                            f"{API_KEY_HASH_ALGORITHM} with at least "
                            f"{API_KEY_HASH_ITERATIONS} iterations"
                        ),
                    )
                )
        return issues

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config if isinstance(ctx.config, AuthPluginConfig) else AuthPluginConfig()
        ctx.services["auth"] = AuthService(
            hashed_api_keys=_hashed_api_key_records(config.hashed_api_keys),
            jwt_secret=config.jwt_secret,
            jwt_signing_keys=_jwt_signing_key_records(config.jwt_signing_keys),
            jwt_key_id=config.jwt_key_id,
            jwt_issuer=config.jwt_issuer,
            jwt_audience=config.jwt_audience,
            access_token_ttl_seconds=config.access_token_ttl_seconds,
        )

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        config = ctx.config if isinstance(ctx.config, AuthPluginConfig) else AuthPluginConfig()
        api_key_count = len(_hashed_api_key_records(config.hashed_api_keys))
        jwt_enabled = bool(config.jwt_secret) or bool(config.jwt_signing_keys)
        details = {
            "auth_record_count": api_key_count,
            "jwt_enabled": jwt_enabled,
        }
        if api_key_count == 0 and not jwt_enabled:
            return ctx.health_status(
                "auth",
                HealthState.DEGRADED,
                "auth plugin is enabled without API keys or JWT signing",
                details,
            )
        return ctx.health_status("auth", HealthState.HEALTHY, details=details)


def _hashed_api_key_records(
    records: dict[str, HashedApiKeyRecord] | list[HashedApiKeyRecord],
) -> list[HashedApiKeyRecord]:
    if isinstance(records, list):
        return records

    normalized: list[HashedApiKeyRecord] = []
    for key_id, record in records.items():
        if record.key_id is None:
            record = record.model_copy(update={"key_id": key_id})
        normalized.append(record)
    return normalized


def _jwt_signing_key_records(
    records: dict[str, JwtSigningKeyRecord] | list[JwtSigningKeyRecord],
) -> list[JwtSigningKeyRecord]:
    if isinstance(records, list):
        return records

    normalized: list[JwtSigningKeyRecord] = []
    for key_id, record in records.items():
        if record.key_id is None:
            record = record.model_copy(update={"key_id": key_id})
        normalized.append(record)
    return normalized


def _weak_auth_secret(secret: str) -> bool:
    normalized = secret.strip().lower()
    if len(secret) < MIN_JWT_SECRET_LENGTH:
        return True
    return normalized in WEAK_AUTH_SECRET_VALUES


def _jwt_signing_secrets(config: AuthPluginConfig) -> dict[str, str]:
    records = config.jwt_signing_keys
    if isinstance(records, list):
        return {
            record.key_id or f"key-{index + 1}": record.secret
            for index, record in enumerate(records)
        }
    return {key_id: record.secret for key_id, record in records.items()}


def _hashed_api_key_hashes(config: AuthPluginConfig) -> dict[str, str]:
    records = config.hashed_api_keys
    if isinstance(records, list):
        return {
            record.key_id or f"key-{index + 1}": record.key_hash
            for index, record in enumerate(records)
        }
    return {key_id: record.key_hash for key_id, record in records.items()}


def _api_key_hash_issue(encoded_hash: str) -> str | None:
    try:
        algorithm, encoded_iterations, _encoded_salt, _encoded_digest = encoded_hash.split("$")
        iterations = int(encoded_iterations)
    except (TypeError, ValueError):
        return "api_key_hash_invalid"
    if algorithm != API_KEY_HASH_ALGORITHM:
        return "api_key_hash_invalid"
    if iterations < API_KEY_HASH_ITERATIONS:
        return "api_key_hash_iterations_too_low"
    return None
