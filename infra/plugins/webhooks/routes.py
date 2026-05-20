from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.routing import Route

from infra.plugins.webhooks.dispatcher import WebhookDispatcher
from infra.plugins.webhooks.providers import WebhookProviderError
from infra.plugins.webhooks.store import InMemoryWebhookStore, WebhookStore


def _ensure_post_route_available(app: FastAPI, path: str) -> None:
    for route in app.routes:
        if isinstance(route, Route) and route.path == path and "POST" in (route.methods or set()):
            raise RuntimeError(f"webhook route collision for: {path}")


def install_webhook_routes(
    app: FastAPI,
    dispatcher: WebhookDispatcher,
    *,
    prefix: str = "/webhooks",
    store: WebhookStore | None = None,
) -> WebhookStore:
    route_prefix = prefix.rstrip("/") or ""
    route_path = f"{route_prefix}" + "/{provider}"
    _ensure_post_route_available(app, route_path)
    if dispatcher.durable_store_required and (
        store is None or isinstance(store, InMemoryWebhookStore)
    ):
        raise RuntimeError("webhook route requires a durable WebhookStore")
    missing_providers = set(dispatcher.required_providers) - set(
        dispatcher.provider_registry.names()
    )
    if missing_providers:
        raise RuntimeError(
            "webhook route is missing providers for: " + ", ".join(sorted(missing_providers))
        )
    event_store = store or InMemoryWebhookStore()

    @app.post(route_path)
    async def receive_webhook(provider: str, request: Request) -> JSONResponse:
        raw_body = await request.body()
        headers = dict(request.headers)
        try:
            webhook_provider = dispatcher.provider_registry.get(provider)
        except LookupError:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"status": "unknown_provider"},
            )

        if not webhook_provider.verify(raw_body, headers):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"status": "signature_failed"},
            )

        try:
            event = webhook_provider.build_event(raw_body, headers)
        except WebhookProviderError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"status": exc.status, "detail": exc.detail},
            )

        if not await event_store.record_once(event):
            return JSONResponse(
                content={"status": "duplicate", "event_id": event.id},
            )

        await dispatcher.dispatch(event.type, event.payload)
        return JSONResponse(content={"status": "processed", "event_id": event.id})

    return event_store
