import re
from pathlib import Path
from typing import Literal

PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PluginProjectKind = Literal["service", "provider"]
SUPPORTED_PROVIDER_KINDS = frozenset(
    {
        "ai",
        "notifications",
        "payment",
        "ratelimit",
        "speech",
        "storage",
        "tasks",
        "webhook",
    }
)


def create_plugin_project(
    destination: str | Path,
    plugin_name: str,
    *,
    kind: PluginProjectKind = "service",
    provider_kind: str = "ai",
    overwrite: bool = False,
) -> list[Path]:
    normalized = plugin_name.strip()
    if not PLUGIN_NAME_RE.fullmatch(normalized):
        raise ValueError(
            "plugin name must start with a lowercase letter and contain only "
            "lowercase letters, numbers, or underscores"
        )
    if kind not in {"service", "provider"}:
        raise ValueError("plugin template kind must be one of: service, provider")

    root = Path(destination)
    if root.exists() and not root.is_dir():
        raise FileExistsError(f"Destination exists and is not a directory: {root}")
    if root.exists() and any(root.iterdir()) and not overwrite:
        raise FileExistsError(f"Destination exists and is not empty: {root}")

    if kind == "provider":
        files = _provider_template_files(normalized, provider_kind.strip())
    else:
        files = _service_template_files(normalized)

    written: list[Path] = []
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def _service_template_files(plugin_name: str) -> dict[Path, str]:
    module_name = f"fastapi_infra_{plugin_name}_plugin"
    package_name = f"fastapi-infra-{plugin_name.replace('_', '-')}-plugin"
    class_prefix = class_name(plugin_name)
    env_prefix = plugin_name.upper()
    return {
        Path("README.md"): _render_readme(plugin_name, package_name),
        Path("pyproject.toml"): _render_pyproject(plugin_name, package_name, module_name),
        Path("infra.example.toml"): _render_infra_example(plugin_name),
        Path("src")
        / module_name
        / "__init__.py": _render_plugin_module(
            plugin_name,
            module_name=module_name,
            class_prefix=class_prefix,
            env_prefix=env_prefix,
        ),
        Path("tests/test_plugin.py"): _render_plugin_test(
            plugin_name,
            module_name=module_name,
            class_prefix=class_prefix,
        ),
    }


def _provider_template_files(plugin_name: str, provider_kind: str) -> dict[Path, str]:
    if provider_kind not in SUPPORTED_PROVIDER_KINDS:
        supported = ", ".join(sorted(SUPPORTED_PROVIDER_KINDS))
        raise ValueError(f"provider kind must be one of: {supported}")
    module_name = f"fastapi_infra_{plugin_name}_{provider_kind}_provider"
    package_name = (
        f"fastapi-infra-{plugin_name.replace('_', '-')}-{provider_kind.replace('_', '-')}-provider"
    )
    class_prefix = f"{class_name(plugin_name)}{class_name(provider_kind)}"
    env_prefix = f"{plugin_name}_{provider_kind}".upper()
    if provider_kind == "ratelimit":
        return {
            Path("README.md"): _render_ratelimit_provider_readme(plugin_name, package_name),
            Path("pyproject.toml"): _render_provider_pyproject(
                plugin_name,
                provider_kind,
                package_name,
                module_name,
            ),
            Path("infra.example.toml"): _render_ratelimit_provider_infra_example(
                plugin_name,
            ),
            Path("src")
            / module_name
            / "__init__.py": _render_ratelimit_provider_module(
                plugin_name,
                class_prefix=class_prefix,
                env_prefix=env_prefix,
            ),
            Path("src")
            / module_name
            / "certification.py": _render_provider_certification(
                plugin_name,
                provider_kind,
                env_prefix=env_prefix,
            ),
            Path("tests/test_provider.py"): _render_ratelimit_provider_test(
                plugin_name,
                module_name=module_name,
                class_prefix=class_prefix,
            ),
        }
    if provider_kind == "tasks":
        return {
            Path("README.md"): _render_tasks_provider_readme(plugin_name, package_name),
            Path("pyproject.toml"): _render_provider_pyproject(
                plugin_name,
                provider_kind,
                package_name,
                module_name,
            ),
            Path("infra.example.toml"): _render_tasks_provider_infra_example(plugin_name),
            Path("src")
            / module_name
            / "__init__.py": _render_tasks_provider_module(
                plugin_name,
                class_prefix=class_prefix,
                env_prefix=env_prefix,
            ),
            Path("src")
            / module_name
            / "certification.py": _render_provider_certification(
                plugin_name,
                provider_kind,
                env_prefix=env_prefix,
            ),
            Path("tests/test_provider.py"): _render_tasks_provider_test(
                plugin_name,
                module_name=module_name,
                class_prefix=class_prefix,
            ),
        }
    if provider_kind == "webhook":
        return {
            Path("README.md"): _render_webhook_provider_readme(plugin_name, package_name),
            Path("pyproject.toml"): _render_provider_pyproject(
                plugin_name,
                provider_kind,
                package_name,
                module_name,
            ),
            Path("infra.example.toml"): _render_webhook_provider_infra_example(
                plugin_name,
            ),
            Path("src")
            / module_name
            / "__init__.py": _render_webhook_provider_module(
                plugin_name,
                class_prefix=class_prefix,
                env_prefix=env_prefix,
            ),
            Path("src")
            / module_name
            / "certification.py": _render_provider_certification(
                plugin_name,
                provider_kind,
                env_prefix=env_prefix,
            ),
            Path("tests/test_provider.py"): _render_webhook_provider_test(
                plugin_name,
                module_name=module_name,
                class_prefix=class_prefix,
            ),
        }
    if provider_kind == "notifications":
        return {
            Path("README.md"): _render_notifications_provider_readme(
                plugin_name,
                package_name,
            ),
            Path("pyproject.toml"): _render_provider_pyproject(
                plugin_name,
                provider_kind,
                package_name,
                module_name,
            ),
            Path("infra.example.toml"): _render_notifications_provider_infra_example(
                plugin_name,
            ),
            Path("src")
            / module_name
            / "__init__.py": _render_notifications_provider_module(
                plugin_name,
                class_prefix=class_prefix,
                env_prefix=env_prefix,
            ),
            Path("src")
            / module_name
            / "certification.py": _render_provider_certification(
                plugin_name,
                provider_kind,
                env_prefix=env_prefix,
            ),
            Path("tests/test_provider.py"): _render_notifications_provider_test(
                plugin_name,
                module_name=module_name,
                class_prefix=class_prefix,
            ),
        }
    if provider_kind == "storage":
        return {
            Path("README.md"): _render_storage_provider_readme(plugin_name, package_name),
            Path("pyproject.toml"): _render_provider_pyproject(
                plugin_name,
                provider_kind,
                package_name,
                module_name,
            ),
            Path("infra.example.toml"): _render_storage_provider_infra_example(plugin_name),
            Path("src")
            / module_name
            / "__init__.py": _render_storage_provider_module(
                plugin_name,
                class_prefix=class_prefix,
                env_prefix=env_prefix,
            ),
            Path("src")
            / module_name
            / "certification.py": _render_provider_certification(
                plugin_name,
                provider_kind,
                env_prefix=env_prefix,
            ),
            Path("tests/test_provider.py"): _render_storage_provider_test(
                plugin_name,
                module_name=module_name,
                class_prefix=class_prefix,
            ),
        }
    if provider_kind == "speech":
        return {
            Path("README.md"): _render_speech_provider_readme(plugin_name, package_name),
            Path("pyproject.toml"): _render_provider_pyproject(
                plugin_name,
                provider_kind,
                package_name,
                module_name,
            ),
            Path("infra.example.toml"): _render_speech_provider_infra_example(plugin_name),
            Path("src")
            / module_name
            / "__init__.py": _render_speech_provider_module(
                plugin_name,
                class_prefix=class_prefix,
                env_prefix=env_prefix,
            ),
            Path("src")
            / module_name
            / "certification.py": _render_provider_certification(
                plugin_name,
                provider_kind,
                env_prefix=env_prefix,
            ),
            Path("tests/test_provider.py"): _render_speech_provider_test(
                plugin_name,
                module_name=module_name,
                class_prefix=class_prefix,
            ),
        }
    if provider_kind == "payment":
        return {
            Path("README.md"): _render_payment_provider_readme(plugin_name, package_name),
            Path("pyproject.toml"): _render_provider_pyproject(
                plugin_name,
                provider_kind,
                package_name,
                module_name,
            ),
            Path("infra.example.toml"): _render_payment_provider_infra_example(plugin_name),
            Path("src")
            / module_name
            / "__init__.py": _render_payment_provider_module(
                plugin_name,
                class_prefix=class_prefix,
                env_prefix=env_prefix,
            ),
            Path("src")
            / module_name
            / "certification.py": _render_provider_certification(
                plugin_name,
                provider_kind,
                env_prefix=env_prefix,
            ),
            Path("tests/test_provider.py"): _render_payment_provider_test(
                plugin_name,
                module_name=module_name,
                class_prefix=class_prefix,
            ),
        }
    return {
        Path("README.md"): _render_ai_provider_readme(plugin_name, package_name),
        Path("pyproject.toml"): _render_provider_pyproject(
            plugin_name,
            provider_kind,
            package_name,
            module_name,
        ),
        Path("infra.example.toml"): _render_ai_provider_infra_example(plugin_name),
        Path("src")
        / module_name
        / "__init__.py": _render_ai_provider_module(
            plugin_name,
            class_prefix=class_prefix,
            env_prefix=env_prefix,
        ),
        Path("src")
        / module_name
        / "certification.py": _render_provider_certification(
            plugin_name,
            provider_kind,
            env_prefix=env_prefix,
        ),
        Path("tests/test_provider.py"): _render_ai_provider_test(
            plugin_name,
            module_name=module_name,
            class_prefix=class_prefix,
        ),
    }


def _render_readme(plugin_name: str, package_name: str) -> str:
    return f"""# {package_name}

External `{plugin_name}` plugin for `fastapi-infra`.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
fastapi-infra plugins check {plugin_name} --settings infra.example.toml --lifecycle
fastapi-infra new /tmp/{plugin_name}-api --plugins {plugin_name}
```
"""


def _render_pyproject(plugin_name: str, package_name: str, module_name: str) -> str:
    return f"""[build-system]
requires = ["setuptools>=77.0.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{package_name}"
version = "0.1.0"
description = "External {plugin_name} plugin for fastapi-infra"
readme = "README.md"
requires-python = ">=3.11"
dependencies = ["fastapi-infra>=0.2.0"]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.25.0",
]

[project.entry-points."fastapi_infra.plugins"]
{plugin_name} = "{module_name}:{class_name(plugin_name)}Plugin"

[tool.setuptools.packages.find]
where = ["src"]
include = ["{module_name}*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
"""


def _render_infra_example(plugin_name: str) -> str:
    return f"""[infra.plugins.{plugin_name}]
enabled = true

[infra.plugins.{plugin_name}.config]
endpoint = "memory://{plugin_name}"
"""


def _render_plugin_module(
    plugin_name: str,
    *,
    module_name: str,
    class_prefix: str,
    env_prefix: str,
) -> str:
    service_name = f"{class_prefix}Service"
    config_name = f"{class_prefix}Config"
    plugin_class_name = f"{class_prefix}Plugin"
    return f"""from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.release_checks import PluginReleaseIssue, release_error


class {config_name}(BaseModel):
    endpoint: str = Field(default="memory://{plugin_name}", min_length=1)
    api_key: str | None = None


class {service_name}:
    def __init__(self, config: {config_name}) -> None:
        self.config = config

    async def run(self, payload: str) -> dict[str, Any]:
        return {{"payload": payload, "endpoint": self.config.endpoint}}


class {plugin_class_name}:
    metadata = PluginMetadata(
        name="{plugin_name}",
        version="0.1.0",
        provides=["{plugin_name}"],
    )
    config_model = {config_name}
    manifest_hints = {{
        "env_vars": ["{env_prefix}_API_KEY"],
        "local_config_example": {{"endpoint": "memory://{plugin_name}"}},
        "production_config_example": {{
            "endpoint": "https://{plugin_name}.example.com",
            "api_key": "${{{env_prefix}_API_KEY}}",
        }},
        "service_keys": {{"{plugin_name}": "{module_name}.{service_name}"}},
        "scaffold_files": [
            {{
                "path": "app/{plugin_name}.py",
                "content": (
                    "from __future__ import annotations\\n\\n"
                    "from infra import InfraContext\\n\\n\\n"
                    "async def use_{plugin_name}(infra: InfraContext) -> dict[str, object]:\\n"
                    "    service = infra.require('{plugin_name}')\\n"
                    "    return await service.run('hello')\\n"
                ),
            }}
        ],
        "scaffold_readme_sections": [
            (
                "## {class_prefix} Plugin\\n\\n"
                "This project enables the external `{plugin_name}` plugin. "
                "Keep its package installed in every runtime environment.\\n"
            )
        ],
        "release_check_notes": [
            "Production {plugin_name} must use a non-memory endpoint and {env_prefix}_API_KEY.",
        ],
    }}

    def register(self, ctx: PluginContext) -> None:
        config = ctx.config
        if not isinstance(config, {config_name}):
            config = {config_name}.model_validate(config or {{}})
        ctx.services["{plugin_name}"] = {service_name}(config)

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        return ctx.health_status("{plugin_name}", HealthState.HEALTHY)

    def release_check(
        self,
        settings: InfraSettings,
        config: {config_name},
    ) -> list[PluginReleaseIssue]:
        if config.endpoint.startswith("memory://"):
            return [
                release_error(
                    "memory_endpoint",
                    "production {plugin_name} requires a non-memory endpoint",
                )
            ]
        if not config.api_key:
            return [
                release_error(
                    "missing_api_key",
                    "production {plugin_name} requires api_key",
                )
            ]
        return []


__all__ = ["{config_name}", "{plugin_class_name}", "{service_name}"]
"""


def _render_plugin_test(plugin_name: str, *, module_name: str, class_prefix: str) -> str:
    plugin_class_name = f"{class_prefix}Plugin"
    service_name = f"{class_prefix}Service"
    return f"""import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.conformance import check_plugins_conformance
from infra.plugins.manager import PluginManager
from {module_name} import {plugin_class_name}, {service_name}


def test_plugin_conforms_to_fastapi_infra_contract():
    results = check_plugins_conformance([{plugin_class_name}()])

    assert results[0].valid is True


@pytest.mark.asyncio
async def test_plugin_starts_and_registers_service():
    manager = PluginManager(
        settings=InfraSettings(infra={{"plugins": {{"{plugin_name}": {{"enabled": True}}}}}}),
        plugins=[{plugin_class_name}()],
    )

    await manager.startup()
    try:
        service = manager.get("{plugin_name}")
        assert isinstance(service, {service_name})
        assert manager.health.snapshot()["{plugin_name}"].status is HealthState.HEALTHY
    finally:
        await manager.shutdown()
"""


def _render_ai_provider_readme(provider_name: str, package_name: str) -> str:
    env_prefix = f"{provider_name}_ai".upper()
    return f"""# {package_name}

External AI provider adapter `{provider_name}` for `fastapi-infra`.

This package does not define a full infra plugin. It plugs into the built-in
`ai` plugin through the `fastapi_infra.ai_providers` entry point.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

## App configuration

```toml
[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.ai.config.providers.{provider_name}]
api_key = {{ "$env" = "{env_prefix}_API_KEY" }}
```

Replace the template request methods with the real provider SDK calls, then add
live certification tests and wire them through `fastapi_infra.provider_checks`.
"""


def _render_provider_pyproject(
    provider_name: str,
    provider_kind: str,
    package_name: str,
    module_name: str,
) -> str:
    entry_point_group = (
        "fastapi_infra.notification_providers"
        if provider_kind == "notifications"
        else (
            "fastapi_infra.task_queue_backends"
            if provider_kind == "tasks"
            else (
                "fastapi_infra.ratelimit_backends"
                if provider_kind == "ratelimit"
                else f"fastapi_infra.{provider_kind}_providers"
            )
        )
    )
    return f"""[build-system]
requires = ["setuptools>=77.0.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{package_name}"
version = "0.1.0"
description = "External {provider_name} {provider_kind} provider adapter for fastapi-infra"
readme = "README.md"
requires-python = ">=3.11"
dependencies = ["fastapi-infra>=0.2.0"]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.25.0",
]

[project.entry-points."{entry_point_group}"]
{provider_name} = "{module_name}:create_provider"

[project.entry-points."fastapi_infra.provider_checks"]
{provider_name}_{provider_kind} = "{module_name}.certification:provider_checks"

[tool.setuptools.packages.find]
where = ["src"]
include = ["{module_name}*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
"""


def _render_ai_provider_infra_example(provider_name: str) -> str:
    env_prefix = f"{provider_name}_ai".upper()
    return f"""[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.ai.config.providers.{provider_name}]
api_key = "{env_prefix.lower()}-dev-key"
"""


def _render_ai_provider_module(
    provider_name: str,
    *,
    class_prefix: str,
    env_prefix: str,
) -> str:
    config_name = f"{class_prefix}ProviderConfig"
    provider_class_name = f"{class_prefix}Provider"
    return f"""from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.ai.models import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
)


class {config_name}(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = None
    timeout: float | None = Field(default=None, gt=0)


class {provider_class_name}:
    def __init__(self, config: {config_name}) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "{provider_name}"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        prompt = _last_user_message(request.messages)
        return ChatResponse(
            provider=self.name,
            model=request.model,
            content=f"{provider_name}: {{prompt}}",
            raw={{"base_url": self.config.base_url}},
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        response = await self.chat(request)
        yield ChatChunk(
            provider=self.name,
            model=response.model,
            content=response.content,
        )

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        inputs = request.input if isinstance(request.input, list) else [request.input]
        return EmbeddingResponse(
            provider=self.name,
            model=request.model,
            embeddings=[_embedding_for(text) for text in inputs],
        )

    async def health_check(self) -> HealthStatus:
        if not self.config.api_key:
            return HealthStatus(
                name=self.name,
                status=HealthState.DEGRADED,
                message="{env_prefix}_API_KEY is not configured",
            )
        return HealthStatus(name=self.name, status=HealthState.HEALTHY)


def create_provider(config: Mapping[str, Any]) -> {provider_class_name}:
    return {provider_class_name}({config_name}.model_validate(config or {{}}))


def _last_user_message(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


def _embedding_for(text: str) -> list[float]:
    seed = sum(ord(char) for char in text)
    return [float((seed + index) % 17) / 17.0 for index in range(8)]


__all__ = ["{config_name}", "{provider_class_name}", "create_provider"]
"""


def _render_provider_certification(
    provider_name: str,
    provider_kind: str,
    *,
    env_prefix: str,
) -> str:
    return f"""from infra.provider_certification import ProviderCheck


def provider_checks() -> tuple[ProviderCheck, ...]:
    return (
        ProviderCheck(
            name="{provider_name}-{provider_kind}",
            provider_kind="{provider_kind}",
            provider_name="{provider_name}",
            tests=("test_live_{provider_name}_{provider_kind}",),
            test_path="tests/integration/test_live_{provider_name}_{provider_kind}.py",
            required_env=("{env_prefix}_API_KEY",),
        ),
    )
"""


def _render_ai_provider_test(
    provider_name: str,
    *,
    module_name: str,
    class_prefix: str,
) -> str:
    provider_class_name = f"{class_prefix}Provider"
    return f"""import pytest

from infra.core.health import HealthState
from infra.plugins.ai.models import ChatMessage, ChatRequest, EmbeddingRequest
from {module_name} import {provider_class_name}, create_provider
from {module_name}.certification import provider_checks


@pytest.mark.asyncio
async def test_ai_provider_contract_methods():
    provider = create_provider({{"api_key": "test-key"}})

    assert isinstance(provider, {provider_class_name})
    assert provider.name == "{provider_name}"

    request = ChatRequest(
        model="example-model",
        messages=[ChatMessage(role="user", content="hello")],
    )
    response = await provider.chat(request)
    chunks = [chunk async for chunk in provider.stream_chat(request)]
    embeddings = await provider.embed(
        EmbeddingRequest(model="example-embedding", input=["one", "two"])
    )
    health = await provider.health_check()

    assert response.provider == "{provider_name}"
    assert response.content == "{provider_name}: hello"
    assert chunks[0].content == response.content
    assert len(embeddings.embeddings) == 2
    assert health.status is HealthState.HEALTHY


def test_provider_certification_metadata_matches_entry_point():
    checks = provider_checks()

    assert checks[0].name == "{provider_name}-ai"
    assert checks[0].provider_kind == "ai"
    assert checks[0].provider_name == "{provider_name}"
"""


def _render_payment_provider_readme(provider_name: str, package_name: str) -> str:
    env_prefix = f"{provider_name}_payment".upper()
    return f"""# {package_name}

External payment provider adapter `{provider_name}` for `fastapi-infra`.

This package does not define a full infra plugin. It plugs into the built-in
`payment` plugin through the `fastapi_infra.payment_providers` entry point.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

## App configuration

```toml
[infra.plugins.payment]
enabled = true

[infra.plugins.payment.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.payment.config.providers.{provider_name}]
api_key = {{ "$env" = "{env_prefix}_API_KEY" }}
```

Replace the template checkout/refund methods with real SDK calls, then add live
certification tests and wire them through `fastapi_infra.provider_checks`.
"""


def _render_payment_provider_infra_example(provider_name: str) -> str:
    env_prefix = f"{provider_name}_payment".upper()
    return f"""[infra.plugins.payment]
enabled = true

[infra.plugins.payment.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.payment.config.providers.{provider_name}]
api_key = "{env_prefix.lower()}-dev-key"
"""


def _render_payment_provider_module(
    provider_name: str,
    *,
    class_prefix: str,
    env_prefix: str,
) -> str:
    config_name = f"{class_prefix}ProviderConfig"
    provider_class_name = f"{class_prefix}Provider"
    return f"""from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.payment.models import PaymentCheckout, PaymentRefund


class {config_name}(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, repr=False)
    api_base: str | None = None
    checkout_base_url: str = "https://checkout.example.com"
    timeout: float | None = Field(default=None, gt=0)


class {provider_class_name}:
    def __init__(self, config: {config_name}) -> None:
        self.config = config
        self._checkouts: dict[str, PaymentCheckout] = {{}}
        self._refunds: dict[str, PaymentRefund] = {{}}

    @property
    def name(self) -> str:
        return "{provider_name}"

    async def create_checkout(
        self,
        amount: int,
        currency: str,
        reference: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentCheckout:
        checkout_id = f"{{self.name}}-checkout-{{len(self._checkouts) + 1}}"
        base_url = self.config.checkout_base_url.rstrip("/")
        checkout = PaymentCheckout(
            id=checkout_id,
            amount=amount,
            currency=currency.lower(),
            reference=reference,
            status="pending",
            url=f"{{base_url}}/{{checkout_id}}",
        )
        self._checkouts[checkout_id] = checkout
        return checkout

    async def get_checkout(self, checkout_id: str) -> PaymentCheckout:
        try:
            return self._checkouts[checkout_id]
        except KeyError as exc:
            raise LookupError(f"unknown {provider_name} checkout: {{checkout_id}}") from exc

    async def get_payment_status(self, checkout_id: str) -> str:
        return (await self.get_checkout(checkout_id)).status

    async def create_refund(
        self,
        checkout_id: str,
        amount: int,
        currency: str,
        reference: str | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> PaymentRefund:
        await self.get_checkout(checkout_id)
        refund_id = f"{{self.name}}-refund-{{len(self._refunds) + 1}}"
        refund = PaymentRefund(
            id=refund_id,
            checkout_id=checkout_id,
            amount=amount,
            currency=currency.lower(),
            status="pending",
            reference=reference,
        )
        self._refunds[refund_id] = refund
        return refund

    async def health_check(self) -> HealthStatus:
        if not self.config.api_key:
            return HealthStatus(
                name=self.name,
                status=HealthState.DEGRADED,
                message="{env_prefix}_API_KEY is not configured",
            )
        return HealthStatus(name=self.name, status=HealthState.HEALTHY)


def create_provider(config: Mapping[str, Any]) -> {provider_class_name}:
    return {provider_class_name}({config_name}.model_validate(config or {{}}))


__all__ = ["{config_name}", "{provider_class_name}", "create_provider"]
"""


def _render_payment_provider_test(
    provider_name: str,
    *,
    module_name: str,
    class_prefix: str,
) -> str:
    provider_class_name = f"{class_prefix}Provider"
    return f"""import pytest

from infra.core.health import HealthState
from {module_name} import {provider_class_name}, create_provider
from {module_name}.certification import provider_checks


@pytest.mark.asyncio
async def test_payment_provider_contract_methods():
    provider = create_provider({{"api_key": "test-key"}})

    assert isinstance(provider, {provider_class_name})
    assert provider.name == "{provider_name}"

    checkout = await provider.create_checkout(
        amount=500,
        currency="USD",
        reference="order-1",
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
        metadata={{"plan": "pro"}},
    )
    loaded = await provider.get_checkout(checkout.id)
    status = await provider.get_payment_status(checkout.id)
    refund = await provider.create_refund(
        checkout.id,
        amount=500,
        currency="USD",
        reference="refund-1",
    )
    health = await provider.health_check()

    assert loaded.id == checkout.id
    assert checkout.status == "pending"
    assert checkout.currency == "usd"
    assert status == "pending"
    assert refund.checkout_id == checkout.id
    assert refund.currency == "usd"
    assert health.status is HealthState.HEALTHY


def test_provider_certification_metadata_matches_entry_point():
    checks = provider_checks()

    assert checks[0].name == "{provider_name}-payment"
    assert checks[0].provider_kind == "payment"
    assert checks[0].provider_name == "{provider_name}"
"""


def _render_speech_provider_readme(provider_name: str, package_name: str) -> str:
    env_prefix = f"{provider_name}_speech".upper()
    return f"""# {package_name}

External speech provider adapter `{provider_name}` for `fastapi-infra`.

This package does not define a full infra plugin. It plugs into the built-in
`speech` plugin through the `fastapi_infra.speech_providers` entry point.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

## App configuration

```toml
[infra.plugins.speech]
enabled = true

[infra.plugins.speech.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.speech.config.providers.{provider_name}]
api_key = {{ "$env" = "{env_prefix}_API_KEY" }}
```

Replace the template ASR/TTS methods with real SDK calls, then add live
certification tests and wire them through `fastapi_infra.provider_checks`.
"""


def _render_speech_provider_infra_example(provider_name: str) -> str:
    env_prefix = f"{provider_name}_speech".upper()
    return f"""[infra.plugins.speech]
enabled = true

[infra.plugins.speech.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.speech.config.providers.{provider_name}]
api_key = "{env_prefix.lower()}-dev-key"
"""


def _render_speech_provider_module(
    provider_name: str,
    *,
    class_prefix: str,
    env_prefix: str,
) -> str:
    config_name = f"{class_prefix}ProviderConfig"
    provider_class_name = f"{class_prefix}Provider"
    return f"""from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.speech.models import (
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
)


class {config_name}(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, repr=False)
    api_base: str | None = None
    timeout: float | None = Field(default=None, gt=0)


class {provider_class_name}:
    def __init__(self, config: {config_name}) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "{provider_name}"

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        return TranscriptionResult(
            text=f"{provider_name} transcription {{len(request.audio)}} bytes",
            language=request.language,
            provider=self.name,
            model=request.model,
        )

    async def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult:
        audio = f"{provider_name}:{{request.voice}}:{{request.text}}".encode()
        return SpeechSynthesisResult(
            audio=audio,
            content_type=_content_type(request.format),
            provider=self.name,
            model=request.model,
            format=request.format,
        )

    async def health_check(self) -> HealthStatus:
        if not self.config.api_key:
            return HealthStatus(
                name=self.name,
                status=HealthState.DEGRADED,
                message="{env_prefix}_API_KEY is not configured",
            )
        return HealthStatus(name=self.name, status=HealthState.HEALTHY)


def create_provider(config: Mapping[str, Any]) -> {provider_class_name}:
    return {provider_class_name}({config_name}.model_validate(config or {{}}))


def _content_type(audio_format: str) -> str:
    if audio_format == "mp3":
        return "audio/mpeg"
    if audio_format == "wav":
        return "audio/wav"
    return f"audio/{{audio_format}}"


__all__ = ["{config_name}", "{provider_class_name}", "create_provider"]
"""


def _render_speech_provider_test(
    provider_name: str,
    *,
    module_name: str,
    class_prefix: str,
) -> str:
    provider_class_name = f"{class_prefix}Provider"
    return f"""import pytest

from infra.core.health import HealthState
from infra.plugins.speech.models import SpeechSynthesisRequest, TranscriptionRequest
from {module_name} import {provider_class_name}, create_provider
from {module_name}.certification import provider_checks


@pytest.mark.asyncio
async def test_speech_provider_contract_methods():
    provider = create_provider({{"api_key": "test-key"}})

    assert isinstance(provider, {provider_class_name})
    assert provider.name == "{provider_name}"

    transcription = await provider.transcribe(
        TranscriptionRequest(
            audio=b"audio",
            format="wav",
            language="en",
            model="asr-model",
        )
    )
    synthesis = await provider.synthesize(
        SpeechSynthesisRequest(
            text="hello",
            voice="alloy",
            format="mp3",
            model="tts-model",
        )
    )
    health = await provider.health_check()

    assert transcription.text == "{provider_name} transcription 5 bytes"
    assert transcription.language == "en"
    assert transcription.provider == "{provider_name}"
    assert synthesis.audio == b"{provider_name}:alloy:hello"
    assert synthesis.content_type == "audio/mpeg"
    assert synthesis.provider == "{provider_name}"
    assert health.status is HealthState.HEALTHY


def test_provider_certification_metadata_matches_entry_point():
    checks = provider_checks()

    assert checks[0].name == "{provider_name}-speech"
    assert checks[0].provider_kind == "speech"
    assert checks[0].provider_name == "{provider_name}"
"""


def _render_storage_provider_readme(provider_name: str, package_name: str) -> str:
    env_prefix = f"{provider_name}_storage".upper()
    return f"""# {package_name}

External storage provider adapter `{provider_name}` for `fastapi-infra`.

This package does not define a full infra plugin. It plugs into the built-in
`storage` plugin through the `fastapi_infra.storage_providers` entry point.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

## App configuration

```toml
[infra.plugins.storage]
enabled = true

[infra.plugins.storage.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.storage.config.providers.{provider_name}]
api_key = {{ "$env" = "{env_prefix}_API_KEY" }}
bucket = "app-assets"
```

Replace the template object methods with real SDK calls, then add live
certification tests and wire them through `fastapi_infra.provider_checks`.
"""


def _render_storage_provider_infra_example(provider_name: str) -> str:
    env_prefix = f"{provider_name}_storage".upper()
    return f"""[infra.plugins.storage]
enabled = true

[infra.plugins.storage.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.storage.config.providers.{provider_name}]
api_key = "{env_prefix.lower()}-dev-key"
bucket = "{provider_name}-dev-bucket"
"""


def _render_storage_provider_module(
    provider_name: str,
    *,
    class_prefix: str,
    env_prefix: str,
) -> str:
    config_name = f"{class_prefix}ProviderConfig"
    provider_class_name = f"{class_prefix}Provider"
    return f"""from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from infra.core.health import HealthState, HealthStatus


class {config_name}(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, repr=False)
    bucket: str = Field(default="{provider_name}-bucket", min_length=1)
    endpoint_url: str | None = None
    public_base_url: str = "https://storage.example.com"
    timeout: float | None = Field(default=None, gt=0)


@dataclass(frozen=True)
class _StoredObject:
    data: bytes
    content_type: str | None = None
    metadata: dict[str, str] | None = None


class {provider_class_name}:
    def __init__(self, config: {config_name}) -> None:
        self.config = config
        self._objects: dict[str, _StoredObject] = {{}}

    @property
    def name(self) -> str:
        return "{provider_name}"

    async def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._objects[_normalize_key(key)] = _StoredObject(
            data=bytes(data),
            content_type=content_type,
            metadata=dict(metadata or {{}}),
        )

    async def get_object(self, key: str) -> bytes:
        normalized = _normalize_key(key)
        try:
            return self._objects[normalized].data
        except KeyError as exc:
            raise FileNotFoundError(normalized) from exc

    async def exists(self, key: str) -> bool:
        return _normalize_key(key) in self._objects

    async def delete_object(self, key: str) -> None:
        self._objects.pop(_normalize_key(key), None)

    async def list_objects(self, prefix: str = "") -> list[str]:
        normalized_prefix = _normalize_prefix(prefix)
        return sorted(key for key in self._objects if key.startswith(normalized_prefix))

    def presign_get_url(self, key: str, expires_seconds: int = 3600) -> str:
        normalized = _normalize_key(key)
        base_url = self.config.public_base_url.rstrip("/")
        bucket = quote(self.config.bucket.strip("/"), safe="")
        quoted_key = quote(normalized, safe="/")
        return f"{{base_url}}/{{bucket}}/{{quoted_key}}?expires={{expires_seconds}}"

    async def health_check(self) -> HealthStatus:
        if not self.config.api_key:
            return HealthStatus(
                name=self.name,
                status=HealthState.DEGRADED,
                message="{env_prefix}_API_KEY is not configured",
            )
        return HealthStatus(name=self.name, status=HealthState.HEALTHY)


def create_provider(config: Mapping[str, Any]) -> {provider_class_name}:
    return {provider_class_name}({config_name}.model_validate(config or {{}}))


def _normalize_key(key: str) -> str:
    normalized = key.strip("/")
    if not normalized or ".." in normalized.split("/"):
        raise ValueError("storage key must be a relative object key")
    return normalized


def _normalize_prefix(prefix: str) -> str:
    if not prefix:
        return ""
    return _normalize_key(prefix)


__all__ = ["{config_name}", "{provider_class_name}", "create_provider"]
"""


def _render_storage_provider_test(
    provider_name: str,
    *,
    module_name: str,
    class_prefix: str,
) -> str:
    provider_class_name = f"{class_prefix}Provider"
    return f"""import pytest

from infra.core.health import HealthState
from {module_name} import {provider_class_name}, create_provider
from {module_name}.certification import provider_checks


@pytest.mark.asyncio
async def test_storage_provider_contract_methods():
    provider = create_provider({{"api_key": "test-key", "bucket": "assets"}})

    assert isinstance(provider, {provider_class_name})
    assert provider.name == "{provider_name}"

    await provider.put_object(
        "avatars/user-1.txt",
        b"hello",
        content_type="text/plain",
        metadata={{"owner": "user-1"}},
    )

    assert await provider.exists("avatars/user-1.txt") is True
    assert await provider.get_object("avatars/user-1.txt") == b"hello"
    assert await provider.list_objects("avatars") == ["avatars/user-1.txt"]
    assert provider.presign_get_url("avatars/user-1.txt").startswith(
        "https://storage.example.com/assets/avatars/user-1.txt?expires="
    )

    await provider.delete_object("avatars/user-1.txt")
    health = await provider.health_check()

    assert await provider.exists("avatars/user-1.txt") is False
    assert health.status is HealthState.HEALTHY


def test_provider_certification_metadata_matches_entry_point():
    checks = provider_checks()

    assert checks[0].name == "{provider_name}-storage"
    assert checks[0].provider_kind == "storage"
    assert checks[0].provider_name == "{provider_name}"
"""


def _render_notifications_provider_readme(provider_name: str, package_name: str) -> str:
    env_prefix = f"{provider_name}_notifications".upper()
    return f"""# {package_name}

External notifications provider adapter `{provider_name}` for `fastapi-infra`.

This package does not define a full infra plugin. It plugs into the built-in
`notifications` plugin through the `fastapi_infra.notification_providers` entry point.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

## App configuration

```toml
[infra.plugins.notifications]
enabled = true

[infra.plugins.notifications.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.notifications.config.providers.{provider_name}]
api_key = {{ "$env" = "{env_prefix}_API_KEY" }}
```

Replace the template send method with real SMS, email, push, or chat API calls,
then add live certification tests and wire them through
`fastapi_infra.provider_checks`.
"""


def _render_notifications_provider_infra_example(provider_name: str) -> str:
    env_prefix = f"{provider_name}_notifications".upper()
    return f"""[infra.plugins.notifications]
enabled = true

[infra.plugins.notifications.config]
default_provider = "{provider_name}"
health_probe = true

[infra.plugins.notifications.config.providers.{provider_name}]
api_key = "{env_prefix.lower()}-dev-key"
"""


def _render_notifications_provider_module(
    provider_name: str,
    *,
    class_prefix: str,
    env_prefix: str,
) -> str:
    config_name = f"{class_prefix}ProviderConfig"
    provider_class_name = f"{class_prefix}Provider"
    return f"""from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from infra.core.health import HealthState, HealthStatus
from infra.plugins.notifications import NotificationResult


class {config_name}(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, repr=False)
    api_base: str | None = None
    default_channel: str = "email"
    timeout: float | None = Field(default=None, gt=0)


class {provider_class_name}:
    def __init__(self, config: {config_name}) -> None:
        self.config = config
        self.results: list[NotificationResult] = []

    @property
    def name(self) -> str:
        return "{provider_name}"

    async def send(
        self,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        result = NotificationResult(
            id=f"{provider_name}_{{uuid4().hex}}",
            channel=channel or self.config.default_channel,
            recipient=recipient,
            subject=subject,
            body=body,
            metadata={{**(metadata or {{}}), "provider": self.name}},
            status="sent",
        )
        self.results.append(result)
        return result

    async def health_check(self) -> HealthStatus:
        if not self.config.api_key:
            return HealthStatus(
                name=self.name,
                status=HealthState.DEGRADED,
                message="{env_prefix}_API_KEY is not configured",
            )
        return HealthStatus(name=self.name, status=HealthState.HEALTHY)


def create_provider(config: Mapping[str, Any]) -> {provider_class_name}:
    return {provider_class_name}({config_name}.model_validate(config or {{}}))


__all__ = ["{config_name}", "{provider_class_name}", "create_provider"]
"""


def _render_notifications_provider_test(
    provider_name: str,
    *,
    module_name: str,
    class_prefix: str,
) -> str:
    provider_class_name = f"{class_prefix}Provider"
    return f"""import pytest

from infra.core.health import HealthState
from {module_name} import {provider_class_name}, create_provider
from {module_name}.certification import provider_checks


@pytest.mark.asyncio
async def test_notifications_provider_contract_methods():
    provider = create_provider({{"api_key": "test-key"}})

    assert isinstance(provider, {provider_class_name})
    assert provider.name == "{provider_name}"

    result = await provider.send(
        "email",
        "user@example.com",
        "Welcome",
        "Hello",
        {{"template": "welcome"}},
    )
    health = await provider.health_check()

    assert result.channel == "email"
    assert result.recipient == "user@example.com"
    assert result.subject == "Welcome"
    assert result.body == "Hello"
    assert result.metadata == {{"template": "welcome", "provider": "{provider_name}"}}
    assert result.status == "sent"
    assert provider.results == [result]
    assert health.status is HealthState.HEALTHY


def test_provider_certification_metadata_matches_entry_point():
    checks = provider_checks()

    assert checks[0].name == "{provider_name}-notifications"
    assert checks[0].provider_kind == "notifications"
    assert checks[0].provider_name == "{provider_name}"
"""


def _render_webhook_provider_readme(provider_name: str, package_name: str) -> str:
    env_prefix = f"{provider_name}_webhook".upper()
    return f"""# {package_name}

External webhook provider adapter `{provider_name}` for `fastapi-infra`.

This package does not define a full infra plugin. It plugs into the built-in
`webhooks` plugin through the `fastapi_infra.webhook_providers` entry point.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

## App configuration

```toml
[infra.plugins.webhooks]
enabled = true

[infra.plugins.webhooks.config]
durable_store = true
required_providers = ["{provider_name}"]

[infra.plugins.webhooks.config.providers.{provider_name}]
webhook_secret = {{ "$env" = "{env_prefix}_SECRET" }}
```

Replace the template signature verifier and event builder with the real provider
rules, then add live certification tests and wire them through
`fastapi_infra.provider_checks`.
"""


def _render_webhook_provider_infra_example(provider_name: str) -> str:
    env_prefix = f"{provider_name}_webhook".upper()
    return f"""[infra.plugins.webhooks]
enabled = true

[infra.plugins.webhooks.config]
durable_store = true
required_providers = ["{provider_name}"]

[infra.plugins.webhooks.config.providers.{provider_name}]
webhook_secret = "{env_prefix.lower()}-dev-secret"
signature_header = "x-{provider_name}-signature"
"""


def _render_webhook_provider_module(
    provider_name: str,
    *,
    class_prefix: str,
    env_prefix: str,
) -> str:
    config_name = f"{class_prefix}ProviderConfig"
    provider_class_name = f"{class_prefix}Provider"
    return f"""from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from infra.plugins.webhooks.models import WebhookEvent
from infra.plugins.webhooks.providers import WebhookProviderError


class {config_name}(BaseModel):
    model_config = ConfigDict(extra="forbid")

    webhook_secret: str | None = Field(default=None, repr=False)
    signature_header: str = "x-{provider_name}-signature"
    event_id_field: str = "id"
    event_type_field: str = "type"


class {provider_class_name}:
    def __init__(self, config: {config_name}) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "{provider_name}"

    def verify(self, payload: bytes, headers: Mapping[str, str]) -> bool:
        if not self.config.webhook_secret:
            return True
        signature = _header_value(headers, self.config.signature_header)
        if signature is None:
            return False
        expected = hmac.new(
            self.config.webhook_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        signature = signature.removeprefix("sha256=")
        return hmac.compare_digest(expected, signature)

    def build_event(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        decoded = _decode_json(payload)
        return WebhookEvent(
            id=_required_text(decoded, self.config.event_id_field),
            provider=self.name,
            type=_required_text(decoded, self.config.event_type_field),
            payload=dict(decoded),
            headers=dict(headers),
        )


def create_provider(config: Mapping[str, Any]) -> {provider_class_name}:
    return {provider_class_name}({config_name}.model_validate(config or {{}}))


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    for header_name, value in headers.items():
        if header_name.lower() == name.lower():
            return value
    return None


def _decode_json(payload: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookProviderError("bad_json", "webhook payload must be JSON") from exc
    if not isinstance(decoded, dict):
        raise WebhookProviderError("bad_json", "webhook payload must be a JSON object")
    return decoded


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if isinstance(value, str) and value:
        return value
    raise WebhookProviderError("invalid_event", f"webhook payload missing {{field}}")


__all__ = ["{config_name}", "{provider_class_name}", "create_provider"]
"""


def _render_webhook_provider_test(
    provider_name: str,
    *,
    module_name: str,
    class_prefix: str,
) -> str:
    provider_class_name = f"{class_prefix}Provider"
    return f"""import hashlib
import hmac
import json

from {module_name} import {provider_class_name}, create_provider
from {module_name}.certification import provider_checks


def test_webhook_provider_contract_methods():
    provider = create_provider({{"webhook_secret": "test-secret"}})

    assert isinstance(provider, {provider_class_name})
    assert provider.name == "{provider_name}"

    payload = json.dumps({{"id": "evt_1", "type": "invoice.paid"}}).encode()
    signature = hmac.new(b"test-secret", payload, hashlib.sha256).hexdigest()

    assert provider.verify(payload, {{"x-{provider_name}-signature": signature}}) is True

    event = provider.build_event(payload, {{"x-{provider_name}-signature": signature}})

    assert event.id == "evt_1"
    assert event.provider == "{provider_name}"
    assert event.type == "invoice.paid"
    assert event.payload == {{"id": "evt_1", "type": "invoice.paid"}}


def test_provider_certification_metadata_matches_entry_point():
    checks = provider_checks()

    assert checks[0].name == "{provider_name}-webhook"
    assert checks[0].provider_kind == "webhook"
    assert checks[0].provider_name == "{provider_name}"
"""


def _render_tasks_provider_readme(provider_name: str, package_name: str) -> str:
    env_prefix = f"{provider_name}_tasks".upper()
    return f"""# {package_name}

External task queue backend `{provider_name}` for `fastapi-infra`.

This package does not define a full infra plugin. It plugs into the built-in
`tasks` plugin through the `fastapi_infra.task_queue_backends` entry point.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

## App configuration

```toml
[infra.plugins.tasks]
enabled = true

[infra.plugins.tasks.config]
default_provider = "{provider_name}"

[infra.plugins.tasks.config.providers.{provider_name}]
api_key = {{ "$env" = "{env_prefix}_API_KEY" }}
queue_name = "default"
```

Replace the template queue methods with real backend calls, then add live
certification tests and wire them through `fastapi_infra.provider_checks`.
"""


def _render_tasks_provider_infra_example(provider_name: str) -> str:
    env_prefix = f"{provider_name}_tasks".upper()
    return f"""[infra.plugins.tasks]
enabled = true

[infra.plugins.tasks.config]
default_provider = "{provider_name}"

[infra.plugins.tasks.config.providers.{provider_name}]
api_key = "{env_prefix.lower()}-dev-key"
queue_name = "{provider_name}-default"
"""


def _render_tasks_provider_module(
    provider_name: str,
    *,
    class_prefix: str,
    env_prefix: str,
) -> str:
    config_name = f"{class_prefix}ProviderConfig"
    provider_class_name = f"{class_prefix}Provider"
    return f"""from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from infra.plugins.tasks.models import TaskEnvelope


class {config_name}(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, repr=False)
    queue_name: str = Field(default="{provider_name}-default", min_length=1)
    visibility_timeout_seconds: float = Field(default=30.0, gt=0)


class {provider_class_name}:
    name = "{provider_name}"

    def __init__(
        self,
        config: {config_name},
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self._queued: deque[str] = deque()
        self._tasks: dict[str, TaskEnvelope] = {{}}
        self._idempotency_keys: dict[str, str] = {{}}
        self._now = now or time.time

    async def enqueue(
        self,
        name: str,
        payload: dict[str, object] | None = None,
        *,
        idempotency_key: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int = 1,
    ) -> TaskEnvelope:
        normalized_key = _normalize_idempotency_key(idempotency_key)
        if normalized_key is not None:
            existing_id = self._idempotency_keys.get(normalized_key)
            if existing_id is not None:
                return self.get(existing_id)

        task = TaskEnvelope(
            name=name,
            payload=payload,
            idempotency_key=normalized_key,
            max_attempts=max_attempts,
            available_at=self._now() + max(0, delay_seconds),
        )
        self._tasks[task.id] = task
        if normalized_key is not None:
            self._idempotency_keys[normalized_key] = task.id
        self._queued.append(task.id)
        return task.model_copy(deep=True)

    async def dequeue(self) -> TaskEnvelope | None:
        now = self._now()
        for _ in range(len(self._queued)):
            task_id = self._queued.popleft()
            task = self._tasks.get(task_id)
            if task is None or task.state != "queued":
                continue
            if task.available_at > now:
                self._queued.append(task_id)
                continue
            task.state = "running"
            task.attempts += 1
            return task.model_copy(deep=True)
        return None

    async def complete(self, task_id: str) -> None:
        task = self._tasks[task_id]
        task.state = "completed"
        task.error = None

    async def fail(self, task_id: str, reason: str) -> None:
        task = self._tasks[task_id]
        task.state = "failed"
        task.error = reason

    async def retry(
        self,
        task_id: str,
        reason: str,
        *,
        delay_seconds: float = 0,
    ) -> None:
        task = self._tasks[task_id]
        task.state = "queued"
        task.error = reason
        task.available_at = self._now() + max(0, delay_seconds)
        self._queued.append(task_id)

    async def dead_letter(self, task_id: str, reason: str) -> None:
        task = self._tasks[task_id]
        task.state = "dead_lettered"
        task.error = reason

    def get(self, task_id: str) -> TaskEnvelope:
        return self._tasks[task_id].model_copy(deep=True)

    async def health_check(self) -> bool:
        return bool(self.config.api_key)


def create_provider(config: dict[str, Any]) -> {provider_class_name}:
    return {provider_class_name}({config_name}.model_validate(config or {{}}))


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError("idempotency_key must not be empty")
    return normalized


__all__ = ["{config_name}", "{provider_class_name}", "create_provider"]
"""


def _render_tasks_provider_test(
    provider_name: str,
    *,
    module_name: str,
    class_prefix: str,
) -> str:
    provider_class_name = f"{class_prefix}Provider"
    return f"""import pytest

from {module_name} import {provider_class_name}, create_provider
from {module_name}.certification import provider_checks


@pytest.mark.asyncio
async def test_tasks_provider_contract_methods():
    provider = create_provider({{"api_key": "test-key"}})

    assert isinstance(provider, {provider_class_name})
    assert provider.name == "{provider_name}"

    first = await provider.enqueue(
        "send_email",
        {{"to": "user@example.com"}},
        idempotency_key="email:user-1",
        max_attempts=2,
    )
    duplicate = await provider.enqueue(
        "send_email",
        {{"to": "user@example.com"}},
        idempotency_key="email:user-1",
    )
    running = await provider.dequeue()

    assert duplicate.id == first.id
    assert running is not None
    assert running.id == first.id
    assert running.state == "running"
    assert running.attempts == 1

    await provider.retry(running.id, "temporary failure", delay_seconds=0)
    retried = await provider.dequeue()

    assert retried is not None
    assert retried.attempts == 2

    await provider.complete(retried.id)

    assert provider.get(first.id).state == "completed"
    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_tasks_provider_dead_letter_and_fail_paths():
    provider = create_provider({{"api_key": "test-key"}})

    failed = await provider.enqueue("fail_me")
    await provider.fail(failed.id, "failed")

    dead = await provider.enqueue("dead_me")
    await provider.dead_letter(dead.id, "dead")

    assert provider.get(failed.id).state == "failed"
    assert provider.get(dead.id).state == "dead_lettered"


def test_provider_certification_metadata_matches_entry_point():
    checks = provider_checks()

    assert checks[0].name == "{provider_name}-tasks"
    assert checks[0].provider_kind == "tasks"
    assert checks[0].provider_name == "{provider_name}"
"""


def _render_ratelimit_provider_readme(provider_name: str, package_name: str) -> str:
    env_prefix = f"{provider_name}_ratelimit".upper()
    return f"""# {package_name}

External rate-limit backend `{provider_name}` for `fastapi-infra`.

This package does not define a full infra plugin. It plugs into the built-in
`ratelimit` plugin through the `fastapi_infra.ratelimit_backends` entry point.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

## App configuration

```toml
[infra.plugins.ratelimit]
enabled = true

[infra.plugins.ratelimit.config]
default_provider = "{provider_name}"

[infra.plugins.ratelimit.config.providers.{provider_name}]
api_key = {{ "$env" = "{env_prefix}_API_KEY" }}
key_prefix = "app"
```

Replace the template limiter with real backend calls, then add live
certification tests and wire them through `fastapi_infra.provider_checks`.
"""


def _render_ratelimit_provider_infra_example(provider_name: str) -> str:
    env_prefix = f"{provider_name}_ratelimit".upper()
    return f"""[infra.plugins.ratelimit]
enabled = true

[infra.plugins.ratelimit.config]
default_provider = "{provider_name}"

[infra.plugins.ratelimit.config.providers.{provider_name}]
api_key = "{env_prefix.lower()}-dev-key"
key_prefix = "{provider_name}:ratelimit"
"""


def _render_ratelimit_provider_module(
    provider_name: str,
    *,
    class_prefix: str,
    env_prefix: str,
) -> str:
    config_name = f"{class_prefix}ProviderConfig"
    provider_class_name = f"{class_prefix}Provider"
    return f"""from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class {config_name}(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, repr=False)
    key_prefix: str = Field(default="{provider_name}:ratelimit", min_length=1)


class {provider_class_name}:
    name = "{provider_name}"

    def __init__(
        self,
        config: {config_name},
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self._now = now or time.monotonic
        self._hits: dict[str, list[float]] = {{}}

    async def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        if limit <= 0:
            return False
        normalized_key = self._key_for(key)
        now = self._now()
        window_start = now - max(window_seconds, 0)
        hits = [hit for hit in self._hits.get(normalized_key, []) if hit > window_start]
        if len(hits) >= limit:
            self._hits[normalized_key] = hits
            return False
        hits.append(now)
        self._hits[normalized_key] = hits
        return True

    async def health_check(self) -> bool:
        return bool(self.config.api_key)

    def _key_for(self, key: str) -> str:
        normalized = key.strip()
        if not normalized:
            raise ValueError("rate limit key must not be empty")
        return f"{{self.config.key_prefix}}:{{normalized}}"


def create_provider(config: dict[str, Any]) -> {provider_class_name}:
    return {provider_class_name}({config_name}.model_validate(config or {{}}))


__all__ = ["{config_name}", "{provider_class_name}", "create_provider"]
"""


def _render_ratelimit_provider_test(
    provider_name: str,
    *,
    module_name: str,
    class_prefix: str,
) -> str:
    provider_class_name = f"{class_prefix}Provider"
    return f"""import pytest

from {module_name} import {provider_class_name}, create_provider
from {module_name}.certification import provider_checks


@pytest.mark.asyncio
async def test_ratelimit_provider_contract_methods():
    provider = create_provider({{"api_key": "test-key", "key_prefix": "tests"}})

    assert isinstance(provider, {provider_class_name})
    assert provider.name == "{provider_name}"

    assert await provider.allow("client:1", limit=2, window_seconds=60) is True
    assert await provider.allow("client:1", limit=2, window_seconds=60) is True
    assert await provider.allow("client:1", limit=2, window_seconds=60) is False
    assert await provider.allow("client:2", limit=2, window_seconds=60) is True
    assert await provider.allow("client:3", limit=0, window_seconds=60) is False
    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_ratelimit_provider_rejects_blank_keys():
    provider = create_provider({{"api_key": "test-key"}})

    with pytest.raises(ValueError, match="rate limit key must not be empty"):
        await provider.allow(" ", limit=1, window_seconds=60)


def test_provider_certification_metadata_matches_entry_point():
    checks = provider_checks()

    assert checks[0].name == "{provider_name}-ratelimit"
    assert checks[0].provider_kind == "ratelimit"
    assert checks[0].provider_name == "{provider_name}"
"""


def class_name(plugin_name: str) -> str:
    return "".join(part.capitalize() for part in plugin_name.split("_"))
