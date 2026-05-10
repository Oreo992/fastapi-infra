import pytest

from infra.config.models import InfraSettings
from infra.exceptions.base import AuthenticationError, AuthorizationError
from infra.plugins.auth import ApiKeyRecord, AuthPlugin, AuthService
from infra.plugins.manager import PluginManager


def test_auth_service_authenticates_known_api_key():
    service = AuthService(
        api_keys={
            "secret": ApiKeyRecord(
                subject="user-1",
                scopes={"read:items"},
                claims={"tenant": "acme"},
            )
        }
    )

    principal = service.authenticate_api_key("secret")

    assert principal.subject == "user-1"
    assert principal.scopes == {"read:items"}
    assert principal.claims == {"tenant": "acme"}


@pytest.mark.parametrize("api_key", [None, "unknown"])
def test_auth_service_rejects_missing_or_unknown_api_key(api_key):
    service = AuthService(api_keys={"secret": ApiKeyRecord(subject="user-1")})

    with pytest.raises(AuthenticationError):
        service.authenticate_api_key(api_key)


def test_auth_service_rejects_missing_required_scope():
    service = AuthService()
    service.add_api_key(
        "secret",
        ApiKeyRecord(subject="user-1", scopes={"read:items"}),
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
                        "api_keys": {
                            "secret": {
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
    token = service.issue_jwt(subject="user-2", scopes=set(), roles={"admin"})
    principal = service.authenticate_bearer(token)
    assert principal.subject == "user-2"
    assert principal.roles == {"admin"}
    assert principal.claims["iss"] == "fastapi-infra"
    assert principal.claims["aud"] == "api"
