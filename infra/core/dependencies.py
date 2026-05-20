from collections.abc import Callable
from typing import Any, TypeVar, overload

from fastapi import Request

from infra.core.context import InfraContext
from infra.core.services import ServiceKey

_MISSING = object()
T = TypeVar("T")


def get_infra(request: Request) -> InfraContext:
    infra = getattr(request.app.state, "infra", None)
    if not isinstance(infra, InfraContext):
        raise RuntimeError(
            "infra is not configured on this FastAPI app; call setup_infra(app, settings)"
        )
    return infra


@overload
def infra_service(name: ServiceKey[T]) -> Callable[[Request], T]: ...


@overload
def infra_service(name: ServiceKey[T], *, default: T) -> Callable[[Request], T]: ...


@overload
def infra_service(name: str, *, default: Any = _MISSING) -> Callable[[Request], Any]: ...


def infra_service(
    name: str | ServiceKey[T],
    *,
    default: Any = _MISSING,
) -> Callable[[Request], Any]:
    service_name = _service_name(name)

    def dependency(request: Request) -> Any:
        infra = get_infra(request)
        if default is not _MISSING:
            service = infra.get(service_name, _MISSING)
            if service is not _MISSING:
                if isinstance(name, ServiceKey):
                    return name.validate(service)
                return service
            return default
        return infra.require(name)

    return dependency


def _service_name(name: str | ServiceKey[Any]) -> str:
    if isinstance(name, ServiceKey):
        return name.name
    service_name = name.strip()
    if not service_name:
        raise ValueError("service name must not be empty")
    return service_name
