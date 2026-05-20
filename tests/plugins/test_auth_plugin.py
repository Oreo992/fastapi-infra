import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.exceptions.base import AuthenticationError, AuthorizationError
from infra.plugins.auth import (
    AuthPlugin,
    AuthService,
    HashedApiKeyRecord,
    JwtSigningKeyRecord,
    hash_api_key,
    verify_api_key_hash,
)
from infra.plugins.manager import PluginManager


def test_hash_api_key_roundtrip_uses_encoded_metadata():
    encoded = hash_api_key("secret", salt=b"fixed-salt")

    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_api_key_hash("secret", encoded) is True
    assert verify_api_key_hash("wrong", encoded) is False


def test_auth_service_authenticates_hashed_api_key():
    service = AuthService(
        hashed_api_keys=[
            HashedApiKeyRecord(
                key_id="primary",
                key_hash=hash_api_key("secret", salt=b"fixed-salt"),
                subject="user-1",
                scopes={"read:items"},
                roles={"service"},
                claims={"tenant": "acme"},
            )
        ]
    )

    principal = service.authenticate_api_key("secret")

    assert principal.subject == "user-1"
    assert principal.scopes == {"read:items"}
    assert principal.roles == {"service"}
    assert principal.claims == {"tenant": "acme"}


def test_auth_service_rejects_wrong_hashed_api_key():
    service = AuthService(
        hashed_api_keys=[
            HashedApiKeyRecord(
                key_id="primary",
                key_hash=hash_api_key("secret", salt=b"fixed-salt"),
                subject="user-1",
            )
        ]
    )

    with pytest.raises(AuthenticationError):
        service.authenticate_api_key("wrong")


def test_auth_service_adds_hashed_api_key_for_rotation():
    service = AuthService()
    service.add_hashed_api_key(
        HashedApiKeyRecord(
            key_id="next",
            key_hash=hash_api_key("rotated", salt=b"fixed-salt"),
            subject="user-2",
        )
    )

    assert service.authenticate_api_key("rotated").subject == "user-2"


@pytest.mark.parametrize("api_key", [None, "unknown"])
def test_auth_service_rejects_missing_or_unknown_api_key(api_key):
    service = AuthService(
        hashed_api_keys=[
            HashedApiKeyRecord(
                key_id="primary",
                key_hash=hash_api_key("secret", salt=b"fixed-salt"),
                subject="user-1",
            )
        ]
    )

    with pytest.raises(AuthenticationError):
        service.authenticate_api_key(api_key)


def test_auth_service_rejects_missing_required_scope():
    service = AuthService(
        hashed_api_keys=[
            HashedApiKeyRecord(
                key_id="primary",
                key_hash=hash_api_key("secret", salt=b"fixed-salt"),
                subject="user-1",
                scopes={"read:items"},
            )
        ]
    )
    principal = service.authenticate_api_key("secret")

    with pytest.raises(AuthorizationError):
        service.require_scopes(principal, ["write:items"])


def test_auth_service_issues_and_authenticates_valid_jwt_roundtrip():
    service = AuthService(
        jwt_secret="secret",
        jwt_issuer="fastapi-infra",
        jwt_audience="api",
    )

    token = service.issue_jwt(
        subject="user-1",
        scopes={"read:items"},
        roles={"admin"},
        claims={"tenant": "acme"},
    )
    principal = service.authenticate_jwt(token)

    assert principal.subject == "user-1"
    assert principal.scopes == {"read:items"}
    assert principal.roles == {"admin"}
    assert principal.claims["tenant"] == "acme"
    assert principal.claims["iss"] == "fastapi-infra"
    assert principal.claims["aud"] == "api"


def test_auth_service_authenticates_bearer_authorization_header():
    service = AuthService(jwt_secret="secret")
    token = service.issue_jwt(subject="user-1", scopes={"read:items"}, roles={"reader"})

    principal = service.authenticate_bearer(f"Bearer {token}")

    assert principal.subject == "user-1"
    assert principal.scopes == {"read:items"}
    assert principal.roles == {"reader"}


def test_auth_service_issues_jwt_with_active_key_id_and_accepts_old_key_during_rotation():
    service = AuthService(
        jwt_signing_keys=[
            JwtSigningKeyRecord(key_id="old", secret="old-secret"),
            JwtSigningKeyRecord(key_id="new", secret="new-secret"),
        ],
        jwt_key_id="new",
    )

    old_token = service.issue_jwt(
        subject="user-old",
        scopes={"items:read"},
        roles=set(),
        key_id="old",
    )
    new_token = service.issue_jwt(
        subject="user-new",
        scopes={"items:write"},
        roles=set(),
    )

    assert service.authenticate_jwt(old_token).subject == "user-old"
    assert service.authenticate_jwt(new_token).subject == "user-new"
    assert _decode_jwt_header(new_token)["kid"] == "new"


def test_auth_service_rejects_jwt_signed_by_removed_key():
    issuing_service = AuthService(
        jwt_signing_keys=[JwtSigningKeyRecord(key_id="old", secret="old-secret")],
        jwt_key_id="old",
    )
    token = issuing_service.issue_jwt(subject="user-1", scopes=set(), roles=set())
    verifying_service = AuthService(
        jwt_signing_keys=[JwtSigningKeyRecord(key_id="new", secret="new-secret")],
        jwt_key_id="new",
    )

    with pytest.raises(AuthenticationError):
        verifying_service.authenticate_jwt(token)


def test_auth_service_rejects_expired_jwt():
    service = AuthService(jwt_secret="secret")
    token = service.issue_jwt(subject="user-1", scopes=set(), roles=set(), expires_in_seconds=-1)

    with pytest.raises(AuthenticationError):
        service.authenticate_jwt(token)


def test_auth_service_rejects_missing_required_role():
    service = AuthService(jwt_secret="secret")
    principal = service.authenticate_jwt(
        service.issue_jwt(subject="user-1", scopes=set(), roles={"member"})
    )

    with pytest.raises(AuthorizationError):
        service.require_roles(principal, ["admin"])


def test_auth_service_rejects_jwt_without_configured_secret():
    service = AuthService()

    with pytest.raises(AuthenticationError):
        service.issue_jwt(subject="user-1", scopes=set(), roles=set())

    with pytest.raises(AuthenticationError):
        service.authenticate_jwt("token")


@pytest.mark.asyncio
async def test_auth_plugin_registers_auth_service_with_plugin_manager():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "hashed_api_keys": {
                            "primary": {
                                "key_hash": hash_api_key("secret", salt=b"fixed-salt"),
                                "subject": "user-1",
                                "scopes": ["read:items"],
                                "claims": {"tenant": "acme"},
                            }
                        },
                        "jwt_secret": "secret",
                        "jwt_issuer": "fastapi-infra",
                        "jwt_audience": "api",
                        "access_token_ttl_seconds": 120,
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AuthPlugin()])

    await manager.startup()

    service = manager.get("auth")
    assert isinstance(service, AuthService)
    assert service.authenticate_api_key("secret").subject == "user-1"


@pytest.mark.asyncio
async def test_auth_plugin_registers_jwt_signing_key_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "jwt_key_id": "next",
                        "jwt_signing_keys": {
                            "previous": {"secret": "previous-secret"},
                            "next": {"secret": "next-secret"},
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AuthPlugin()])

    await manager.startup()

    service = manager.get("auth")
    assert isinstance(service, AuthService)
    token = service.issue_jwt(subject="user-1", scopes=set(), roles=set())
    assert service.authenticate_jwt(token).subject == "user-1"
    assert _decode_jwt_header(token)["kid"] == "next"


def _decode_jwt_header(token: str):
    import base64
    import json

    encoded_header = token.split(".", 1)[0]
    padded = encoded_header + "=" * (-len(encoded_header) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


@pytest.mark.asyncio
async def test_auth_plugin_rejects_plaintext_api_key_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "api_keys": {
                            "secret": {
                                "subject": "user-1",
                            }
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AuthPlugin()])

    with pytest.raises(ValueError, match="api_keys"):
        await manager.startup()


@pytest.mark.asyncio
async def test_auth_plugin_registers_hashed_api_key_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "hashed_api_keys": {
                            "primary": {
                                "key_hash": hash_api_key("secret", salt=b"fixed-salt"),
                                "subject": "user-1",
                                "scopes": ["read:items"],
                                "roles": ["service"],
                                "claims": {"tenant": "acme"},
                            }
                        },
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AuthPlugin()])

    await manager.startup()

    service = manager.get("auth")
    assert isinstance(service, AuthService)
    principal = service.authenticate_api_key("secret")
    assert principal.subject == "user-1"
    assert principal.scopes == {"read:items"}
    assert principal.roles == {"service"}
    assert principal.claims == {"tenant": "acme"}


@pytest.mark.asyncio
async def test_auth_plugin_reports_degraded_when_enabled_without_credentials():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {"enabled": True},
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AuthPlugin()])

    await manager.startup()

    status = manager.health.snapshot()["auth"]
    assert status.status is HealthState.DEGRADED
    assert status.message == "auth plugin is enabled without API keys or JWT signing"
    assert status.details == {"auth_record_count": 0, "jwt_enabled": False}


@pytest.mark.asyncio
async def test_auth_plugin_accepts_hashed_api_key_list_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "auth": {
                    "enabled": True,
                    "config": {
                        "hashed_api_keys": [
                            {
                                "key_id": "primary",
                                "key_hash": hash_api_key("secret", salt=b"fixed-salt"),
                                "subject": "user-1",
                            }
                        ],
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AuthPlugin()])

    await manager.startup()

    service = manager.get("auth")
    assert isinstance(service, AuthService)
    assert service.authenticate_api_key("secret").subject == "user-1"
