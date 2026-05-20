from fastapi import FastAPI, Header

from infra import InfraSettings, setup_infra
from infra.plugins import (
    AUTH_SERVICE,
    NOTIFICATIONS_SERVICE,
    PAYMENT_SERVICE,
    TASKS_SERVICE,
)
from infra.plugins.auth import hash_api_key

settings = InfraSettings(
    infra={
        "plugins": {
            "auth": {
                "enabled": True,
                "config": {
                    "hashed_api_keys": {
                        "dev-key": {
                            "key_hash": hash_api_key("dev-key", salt=b"fastapi-infra-example"),
                            "subject": "developer",
                            "scopes": ["checkout:create", "tasks:create"],
                        }
                    }
                },
            },
            "ai": {"enabled": True, "config": {"default_provider": "mock"}},
            "payment": {"enabled": True},
            "tasks": {"enabled": True},
            "notifications": {"enabled": True},
        }
    }
)
app = FastAPI(title="fastapi-infra full stack")
infra = setup_infra(app, settings)


@app.post("/checkout")
async def checkout(payload: dict, x_api_key: str | None = Header(default=None)):
    auth = infra.require(AUTH_SERVICE)
    principal = auth.authenticate_api_key(x_api_key)
    auth.require_scopes(principal, ["checkout:create"])

    payment = infra.require(PAYMENT_SERVICE)
    checkout_result = await payment.create_checkout(
        amount=int(payload["amount"]),
        currency=payload.get("currency", "usd"),
        reference=payload.get("reference"),
    )
    return checkout_result.model_dump()


@app.post("/tasks")
async def create_task(payload: dict, x_api_key: str | None = Header(default=None)):
    auth = infra.require(AUTH_SERVICE)
    principal = auth.authenticate_api_key(x_api_key)
    auth.require_scopes(principal, ["tasks:create"])

    tasks = infra.require(TASKS_SERVICE)
    task = await tasks.enqueue(payload["name"], payload.get("payload"))
    return task.model_dump()


@app.post("/notify")
async def notify(payload: dict):
    notifications = infra.require(NOTIFICATIONS_SERVICE)
    result = await notifications.send(
        channel=payload.get("channel", "email"),
        recipient=payload["recipient"],
        subject=payload.get("subject", "Notification"),
        body=payload.get("body", ""),
    )
    return result.model_dump()
