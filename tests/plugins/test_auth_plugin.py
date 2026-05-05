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
                        }
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
