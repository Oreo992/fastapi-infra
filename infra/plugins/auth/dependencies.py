from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from infra.exceptions.base import AuthenticationError, AuthorizationError
from infra.plugins.auth.models import Principal
from infra.plugins.auth.service import AuthService
from infra.plugins.services import AUTH_SERVICE


def require_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    service = _get_auth_service(request)
    try:
        if authorization:
            scheme, separator, _credentials = authorization.strip().partition(" ")
            if not separator or scheme.lower() != "bearer":
                raise AuthenticationError()
            return service.authenticate_bearer(authorization)
        return service.authenticate_api_key(x_api_key)
    except AuthenticationError as exc:
        raise _authentication_exception() from exc


def require_scopes(*scopes: str) -> Callable[..., Principal]:
    required = tuple(scopes)

    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> Principal:
        service = _get_auth_service(request)
        try:
            service.require_scopes(principal, required)
        except AuthorizationError as exc:
            raise _authorization_exception() from exc
        return principal

    return dependency


def require_roles(*roles: str) -> Callable[..., Principal]:
    required = tuple(roles)

    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> Principal:
        service = _get_auth_service(request)
        try:
            service.require_roles(principal, required)
        except AuthorizationError as exc:
            raise _authorization_exception() from exc
        return principal

    return dependency


def _get_auth_service(request: Request) -> AuthService:
    infra = getattr(request.app.state, "infra", None)
    if infra is None or not hasattr(infra, "get"):
        raise _authentication_exception()
    service: Any = infra.get(AUTH_SERVICE)

    if isinstance(service, AuthService):
        return service

    raise _authentication_exception()


def _authentication_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication failed",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authorization_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="permission denied",
    )
