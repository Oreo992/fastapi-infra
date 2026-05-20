from fastapi import FastAPI

from infra import InfraSettings, setup_infra

app = FastAPI(title="fastapi-infra minimal")
infra = setup_infra(app, InfraSettings())


@app.get("/")
async def root():
    return {"ok": True}


@app.get("/health")
async def health():
    return {name: status.model_dump() for name, status in infra.health.snapshot().items()}
