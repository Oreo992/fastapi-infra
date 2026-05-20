import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.provider_extensions import (
    external_provider_names_to_load,
    load_entry_point_provider,
)
from infra.plugins.provider_health import provider_health_status
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginProviderPolicy,
    PluginReleaseIssue,
    provider_certification,
    provider_policy,
    release_error,
)
from infra.plugins.storage.registry import StorageProviderRegistry
from infra.plugins.storage.s3 import S3Storage, S3StorageConfig

STORAGE_PROVIDER_ENTRY_POINT_GROUP = "fastapi_infra.storage_providers"


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: str = "local"
    root: Path | None = None
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    health_probe: bool = False


class LocalStorage:
    name = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get_object(self, key: str) -> bytes:
        return self._path_for(key).read_bytes()

    async def exists(self, key: str) -> bool:
        return self._path_for(key).exists()

    async def delete_object(self, key: str) -> None:
        path = self._path_for(key)
        if path.exists():
            path.unlink()

    async def list_objects(self, prefix: str = "") -> list[str]:
        self._prefix_path_for(prefix)
        candidates = [path for path in self.root.rglob("*") if path.is_file()]
        keys = [path.relative_to(self.root).as_posix() for path in candidates]
        return sorted(key for key in keys if key.startswith(prefix))

    def presign_get_url(self, key: str, expires_seconds: int = 3600) -> str:
        raise NotImplementedError("LocalStorage does not support presigned URLs")

    def _path_for(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("storage key escapes root")
        if candidate == self.root:
            raise ValueError("storage key must identify a file")
        return candidate

    def _prefix_path_for(self, prefix: str) -> Path:
        candidate = (self.root / prefix).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("storage prefix escapes root")
        return candidate


class StoragePlugin:
    metadata = PluginMetadata(
        name="storage",
        version="1.0.0",
        default_enabled=False,
        provides=["storage"],
    )
    config_model = StorageConfig
    manifest_hints = {
        "service_keys": {"storage": "infra.plugins.STORAGE_SERVICE"},
        "env_vars": [
            "S3_LIVE_BUCKET",
            "S3_LIVE_REGION",
            "S3_LIVE_ACCESS_KEY_ID",
            "S3_LIVE_SECRET_ACCESS_KEY",
            "S3_LIVE_ENDPOINT_URL",
        ],
        "local_config_example": {
            "default_provider": "local",
            "root": ".data/storage",
        },
        "production_config_example": {
            "default_provider": "s3",
            "health_probe": True,
            "providers": {
                "s3": {
                    "bucket": "${S3_LIVE_BUCKET}",
                    "region": "${S3_LIVE_REGION}",
                    "access_key_id": "${S3_LIVE_ACCESS_KEY_ID}",
                    "secret_access_key": "${S3_LIVE_SECRET_ACCESS_KEY}",
                    "endpoint_url": "${S3_LIVE_ENDPOINT_URL}",
                }
            },
        },
        "release_check_notes": [
            "Production cannot use local storage.",
            "S3 requires health_probe=true and provider certification.",
        ],
    }

    def validate_config(self, config: StorageConfig | None) -> None:
        config = config if isinstance(config, StorageConfig) else StorageConfig()
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "local" in provider_names:
            registered_names.add("local")
        if "s3" in provider_names:
            S3StorageConfig.model_validate(config.providers.get("s3", {}))
            registered_names.add("s3")
        external_provider_names_to_load(
            provider_kind="storage",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=STORAGE_PROVIDER_ENTRY_POINT_GROUP,
        )

    def release_check(
        self,
        settings: InfraSettings,
        config: StorageConfig,
    ) -> list[PluginReleaseIssue]:
        issues: list[PluginReleaseIssue] = []
        provider_names = set(config.providers) | {config.default_provider}
        if config.default_provider == "local":
            issues.append(
                release_error(
                    "local_provider",
                    "production storage cannot use local",
                )
            )
        if "s3" in provider_names:
            try:
                S3StorageConfig.model_validate(config.providers.get("s3", {}))
            except (ValidationError, ValueError) as exc:
                issues.append(release_error("s3_config_invalid", str(exc)))
        return issues

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: StorageConfig,
    ) -> list[PluginProviderCertification]:
        return [provider_certification("storage", config.default_provider)]

    def provider_release_policies(
        self,
        settings: InfraSettings,
        config: StorageConfig,
    ) -> list[PluginProviderPolicy]:
        return [
            provider_policy(
                "storage",
                {config.default_provider},
                local_providers={"local"},
                health_probe=config.health_probe,
            )
        ]

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config if isinstance(ctx.config, StorageConfig) else StorageConfig()
        registry = StorageProviderRegistry(default_provider=config.default_provider)
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "local" in provider_names:
            root = config.root or Path(tempfile.mkdtemp(prefix="fastapi-infra-storage-"))
            registry.register(LocalStorage(root), default=config.default_provider == "local")
            registered_names.add("local")
        if "s3" in provider_names:
            s3_config = S3StorageConfig.model_validate(config.providers.get("s3", {}))
            registry.register(S3Storage(s3_config), default=config.default_provider == "s3")
            registered_names.add("s3")
        for provider_name in external_provider_names_to_load(
            provider_kind="storage",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=STORAGE_PROVIDER_ENTRY_POINT_GROUP,
        ):
            registry.register(
                load_entry_point_provider(
                    STORAGE_PROVIDER_ENTRY_POINT_GROUP,
                    provider_name,
                    config.providers.get(provider_name, {}),
                    required_methods=(
                        "put_object",
                        "get_object",
                        "exists",
                        "delete_object",
                        "list_objects",
                    ),
                ),
                default=config.default_provider == provider_name,
            )
        registry.get()
        ctx.services["storage"] = registry

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        config = ctx.config if isinstance(ctx.config, StorageConfig) else StorageConfig()
        storage = ctx.services.get("storage")
        if not isinstance(storage, StorageProviderRegistry):
            return ctx.health_status("storage", HealthState.UNHEALTHY, "storage registry missing")
        if config.default_provider != "local":
            if config.health_probe:
                return await provider_health_status(
                    ctx,
                    "storage",
                    storage.get(config.default_provider),
                    local_provider_names={"local"},
                )
            return ctx.health_status(
                "storage",
                HealthState.DEGRADED,
                "external provider configured; upstream is not checked by health",
                {"provider": config.default_provider},
            )
        return ctx.health_status("storage", HealthState.HEALTHY)
