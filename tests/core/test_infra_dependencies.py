from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from infra import InfraSettings, ServiceKey, get_infra, infra_service, setup_infra
from infra.core.context import InfraContext
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata


class ServicePlugin:
    metadata = PluginMetadata(
        name="service",
        version="1.0.0",
        default_enabled=True,
        provides=["service"],
    )
    config_model = None

    def register(self, ctx: PluginContext) -> None:
        ctx.services["service"] = {"ready": True}

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("service", HealthState.HEALTHY)


def test_get_infra_dependency_returns_configured_context() -> None:
    app = FastAPI()
    infra = setup_infra(app, InfraSettings(), plugins=[ServicePlugin()])

    @app.get("/infra")
    async def route(ctx: Annotated[InfraContext, Depends(get_infra)]):
        return {"configured": ctx is infra}

    with TestClient(app) as client:
        response = client.get("/infra")

    assert response.status_code == 200
    assert response.json() == {"configured": True}


def test_infra_service_dependency_returns_registered_service() -> None:
    app = FastAPI()
    setup_infra(app, InfraSettings(), plugins=[ServicePlugin()])

    @app.get("/service")
    async def route(service: Annotated[dict[str, bool], Depends(infra_service("service"))]):
        return service

    with TestClient(app) as client:
        response = client.get("/service")

    assert response.status_code == 200
    assert response.json() == {"ready": True}


def test_infra_service_dependency_accepts_typed_service_key() -> None:
    app = FastAPI()
    infra = setup_infra(app, InfraSettings(), plugins=[ServicePlugin()])
    service_key = ServiceKey[dict]("service", dict)

    @app.get("/service")
    async def route(service: Annotated[dict[str, bool], Depends(infra_service(service_key))]):
        return service

    with TestClient(app) as client:
        assert infra.get(service_key) == {"ready": True}
        response = client.get("/service")

    assert response.status_code == 200
    assert response.json() == {"ready": True}


def test_infra_context_require_returns_required_service() -> None:
    app = FastAPI()
    infra = setup_infra(app, InfraSettings(), plugins=[ServicePlugin()])
    service_key = ServiceKey[dict]("service", dict)

    with TestClient(app):
        assert infra.require("service") == {"ready": True}
        assert infra.require(service_key) == {"ready": True}


def test_infra_service_dependency_can_return_default_for_missing_service() -> None:
    app = FastAPI()
    setup_infra(app, InfraSettings(), plugins=[ServicePlugin()])

    @app.get("/optional")
    async def route(
        service: Annotated[
            dict[str, bool], Depends(infra_service("missing", default={"ready": False}))
        ],
    ):
        return service

    with TestClient(app) as client:
        response = client.get("/optional")

    assert response.status_code == 200
    assert response.json() == {"ready": False}


def test_typed_service_key_validates_runtime_type() -> None:
    app = FastAPI()
    infra = setup_infra(app, InfraSettings(), plugins=[ServicePlugin()])
    service_key = ServiceKey[list]("service", list)

    with TestClient(app):
        with pytest.raises(RuntimeError, match="unexpected type"):
            infra.get(service_key)


def test_infra_context_require_raises_for_missing_service() -> None:
    app = FastAPI()
    infra = setup_infra(app, InfraSettings(), plugins=[ServicePlugin()])

    with TestClient(app):
        with pytest.raises(RuntimeError, match="infra service is not available: missing"):
            infra.require("missing")


def test_infra_context_require_validates_service_name() -> None:
    app = FastAPI()
    infra = setup_infra(app, InfraSettings(), plugins=[ServicePlugin()])

    with pytest.raises(ValueError, match="service name"):
        infra.require(" ")


def test_infra_service_dependency_raises_for_required_missing_service() -> None:
    app = FastAPI()
    setup_infra(app, InfraSettings(), plugins=[ServicePlugin()])

    @app.get("/missing")
    async def route(service: Annotated[object, Depends(infra_service("missing"))]):
        return {"service": service}

    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="infra service is not available: missing"):
            client.get("/missing")


def test_infra_dependencies_raise_when_infra_is_not_configured() -> None:
    app = FastAPI()

    @app.get("/infra")
    async def route(ctx: Annotated[InfraContext, Depends(get_infra)]):
        return {"configured": bool(ctx)}

    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="setup_infra"):
            client.get("/infra")


def test_infra_service_dependency_validates_service_name() -> None:
    with pytest.raises(ValueError, match="service name"):
        infra_service(" ")

    with pytest.raises(ValueError, match="service name"):
        ServiceKey(" ")
