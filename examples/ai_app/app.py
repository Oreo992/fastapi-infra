from fastapi import FastAPI

from infra import InfraSettings, setup_infra


settings = InfraSettings(
    infra={
        "plugins": {
            "ai": {
                "enabled": True,
                "config": {"default_provider": "mock"},
            }
        }
    }
)
app = FastAPI(title="fastapi-infra ai")
infra = setup_infra(app, settings)


@app.post("/chat")
async def chat(payload: dict):
    ai = infra.get("ai")
    response = await ai.chat_text(payload["message"])
    return response.model_dump()
