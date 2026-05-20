from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from infra.plugins import AUTH_SERVICE
from infra.plugins.auth import (
    AuthService,
    HashedApiKeyRecord,
    Principal,
    hash_api_key,
    require_principal,
    require_roles,
    require_scopes,
)


class FakeInfraContext:
    def __init__(self, service: AuthService) -> None:
        self._service = service

    def get(self, name, default=None):
        service_name = name.name if hasattr(name, "name") else name
        if service_name == AUTH_SERVICE.name:
            return self._service
        return default


def build_client() -> tuple[TestClient, AuthService]:
    service = AuthService(
        jwt_secret="secret",
        hashed_api_keys=[
            HashedApiKeyRecord(
                key_id="primary",
                key_hash=hash_api_key("api-secret", salt=b"fixed-salt"),
                subject="api-user",
                scopes={"items:read"},
                roles={"service"},
                claims={"tenant": "acme"},
            )
        ],
    )
    app = FastAPI()
    app.state.infra = FakeInfraContext(service)

    @app.get("/me")
    def me(principal: Annotated[Principal, Depends(require_principal)]):
        return {
            "subject": principal.subject,
            "scopes": sorted(principal.scopes),
            "roles": sorted(principal.roles),
        }

    @app.get("/items")
    def items(principal: Annotated[Principal, Depends(require_scopes("items:read"))]):
        return {"subject": principal.subject}

    @app.get("/admin")
    def admin(principal: Annotated[Principal, Depends(require_roles("admin"))]):
        return {"subject": principal.subject}

    return TestClient(app), service


def build_hashed_client() -> TestClient:
    service = AuthService()
    service.add_hashed_api_key(
        HashedApiKeyRecord(
            key_id="primary",
            key_hash=hash_api_key("api-secret", salt=b"fixed-salt"),
            subject="hashed-api-user",
            scopes={"items:read"},
            roles={"service"},
        )
    )
    app = FastAPI()
    app.state.infra = FakeInfraContext(service)

    @app.get("/me")
    def me(principal: Annotated[Principal, Depends(require_principal)]):
        return {
            "subject": principal.subject,
            "scopes": sorted(principal.scopes),
            "roles": sorted(principal.roles),
        }

    return TestClient(app)


def test_require_principal_authenticates_bearer_jwt():
    client, service = build_client()
    token = service.issue_jwt(
        subject="user-1",
        scopes={"items:read"},
        roles={"admin"},
    )

    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "subject": "user-1",
        "scopes": ["items:read"],
        "roles": ["admin"],
    }


def test_require_principal_authenticates_api_key():
    client, _service = build_client()

    response = client.get("/me", headers={"X-API-Key": "api-secret"})

    assert response.status_code == 200
    assert response.json() == {
        "subject": "api-user",
        "scopes": ["items:read"],
        "roles": ["service"],
    }


def test_require_principal_authenticates_hashed_api_key():
    client = build_hashed_client()

    response = client.get("/me", headers={"X-API-Key": "api-secret"})

    assert response.status_code == 200
    assert response.json() == {
        "subject": "hashed-api-user",
        "scopes": ["items:read"],
        "roles": ["service"],
    }


def test_require_principal_rejects_missing_credentials():
    client, _service = build_client()

    response = client.get("/me")

    assert response.status_code == 401


def test_require_principal_rejects_invalid_bearer_token():
    client, _service = build_client()

    response = client.get("/me", headers={"Authorization": "Bearer invalid"})

    assert response.status_code == 401


def test_require_principal_rejects_authorization_without_bearer_scheme():
    client, service = build_client()
    token = service.issue_jwt(subject="user-1", scopes=set(), roles=set())

    response = client.get("/me", headers={"Authorization": token})

    assert response.status_code == 401


def test_require_scopes_returns_principal_when_scope_present():
    client, service = build_client()
    token = service.issue_jwt(
        subject="user-1",
        scopes={"items:read"},
        roles=set(),
    )

    response = client.get("/items", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"subject": "user-1"}


def test_require_scopes_rejects_missing_scope():
    client, service = build_client()
    token = service.issue_jwt(
        subject="user-1",
        scopes={"items:write"},
        roles=set(),
    )

    response = client.get("/items", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


def test_require_roles_returns_principal_when_role_present():
    client, service = build_client()
    token = service.issue_jwt(
        subject="user-1",
        scopes=set(),
        roles={"admin"},
    )

    response = client.get("/admin", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"subject": "user-1"}


def test_require_roles_rejects_missing_role():
    client, _service = build_client()

    response = client.get("/admin", headers={"X-API-Key": "api-secret"})

    assert response.status_code == 403
