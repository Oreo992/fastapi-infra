import tempfile
from pathlib import Path

from pydantic import BaseModel

from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata


class StorageConfig(BaseModel):
    root: Path | None = None


class LocalStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def write_bytes(self, key: str, data: bytes) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def read_bytes(self, key: str) -> bytes:
        return self._path_for(key).read_bytes()

    async def exists(self, key: str) -> bool:
        return self._path_for(key).exists()

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        if path.exists():
            path.unlink()

    def _path_for(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("storage key escapes root")
        if candidate == self.root:
            raise ValueError("storage key must identify a file")
        return candidate


class StoragePlugin:
    metadata = PluginMetadata(
        name="storage",
        version="1.0.0",
        provides=["storage"],
    )
    config_model = StorageConfig

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config if isinstance(ctx.config, StorageConfig) else StorageConfig()
        root = config.root or Path(tempfile.mkdtemp(prefix="fastapi-infra-storage-"))
        ctx.services["storage"] = LocalStorage(root)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("storage", HealthState.HEALTHY)
