import pytest
from pydantic import ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.ai import AIPlugin, ChatResponse
from infra.plugins.ai.adapters.openai import OpenAIProvider
from infra.plugins.ai.plugin import AIPluginConfig, AIProviderConfig
from infra.plugins.ai.registry import AIRegistry
from infra.plugins.manager import PluginManager


@pytest.mark.asyncio
async def test_ai_plugin_registers_service_and_chat_text_uses_mock_provider():
    settings = InfraSettings(infra={"plugins": {"ai": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[AIPlugin()])

    await manager.startup()
    registry = manager.get("ai")
    response = await registry.chat_text("hello")
    await manager.shutdown()

    assert registry is not None
    assert response.content == "mock response: hello"
    assert response.provider == "mock"


@pytest.mark.asyncio
async def test_ai_plugin_honors_configured_default_provider_without_sdk_import():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {"default_provider": "openai"},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AIPlugin()])

    await manager.startup()
    registry = manager.get("ai")
    await manager.shutdown()

    assert registry.default_provider == "openai"
    assert registry.get().name == "openai"
    assert manager.health.snapshot()["ai"].status is HealthState.DEGRADED


@pytest.mark.asyncio
async def test_ai_plugin_rejects_unknown_default_provider():
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {"default_provider": "unknown"},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AIPlugin()])

    with pytest.raises(ValueError, match="unknown ai provider"):
        await manager.startup()


def test_ai_plugin_config_accepts_provider_sdk_options():
    config = AIPluginConfig.model_validate(
        {
            "default_provider": "openai",
            "health_probe": True,
            "providers": {
                "openai": {
                    "api_key": "sk-openai",
                    "base_url": "https://openai.test/v1",
                    "timeout": 7.5,
                },
                "anthropic": {
                    "api_key": "sk-anthropic",
                    "base_url": "https://anthropic.test",
                    "timeout": 8.5,
                },
                "gemini": {
                    "api_key": "sk-gemini",
                    "base_url": "https://gemini.test",
                    "timeout": 9.5,
                },
            },
        }
    )

    assert config.providers["openai"].api_key == "sk-openai"
    assert config.health_probe is True
    assert config.providers["openai"].base_url == "https://openai.test/v1"
    assert config.providers["openai"].timeout == 7.5
    assert config.providers["anthropic"].api_key == "sk-anthropic"
    assert config.providers["gemini"].api_key == "sk-gemini"


def test_ai_provider_config_validates_base_url_and_timeout():
    with pytest.raises(ValidationError, match="base_url"):
        AIProviderConfig.model_validate({"base_url": "not-a-url"})
    with pytest.raises(ValidationError, match="timeout"):
        AIProviderConfig.model_validate({"timeout": 0})
    with pytest.raises(ValidationError, match="timeout"):
        AIProviderConfig.model_validate({"timeout": True})


def test_ai_provider_config_strips_and_rejects_blank_api_key():
    config = AIProviderConfig.model_validate({"api_key": "  sk-test  "})

    assert config.api_key == "sk-test"

    with pytest.raises(ValidationError, match="api_key"):
        AIProviderConfig.model_validate({"api_key": "   "})


class CustomAIProvider:
    name = "custom"

    def __init__(self, config):
        self.config = dict(config)

    async def chat(self, request):
        return ChatResponse(
            content=f"custom:{self.config['api_key']}",
            provider=self.name,
            model=request.model,
        )

    def stream_chat(self, request):
        raise NotImplementedError

    async def embed(self, request):
        raise NotImplementedError


class FakeEntryPoint:
    name = "custom"

    def load(self):
        return lambda config: CustomAIProvider(config)


class BrokenAIProvider:
    name = "custom"

    async def chat(self, request):
        return ChatResponse(
            content="broken",
            provider=self.name,
            model=request.model,
        )


class BrokenAIProviderEntryPoint:
    name = "custom"

    def load(self):
        return lambda config: BrokenAIProvider()


@pytest.mark.asyncio
async def test_ai_plugin_registers_custom_entry_point_provider(monkeypatch):
    import infra.plugins.provider_extensions as provider_extensions

    monkeypatch.setattr(
        provider_extensions,
        "entry_points",
        lambda group: [FakeEntryPoint()] if group == "fastapi_infra.ai_providers" else [],
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {"api_key": "sk-custom"}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AIPlugin()])

    await manager.startup()
    registry = manager.get("ai")
    response = await registry.chat_text("hello")
    await manager.shutdown()

    assert response.content == "custom:sk-custom"
    assert response.provider == "custom"


@pytest.mark.asyncio
async def test_ai_plugin_rejects_custom_provider_missing_required_methods(monkeypatch):
    import infra.plugins.provider_extensions as provider_extensions

    monkeypatch.setattr(
        provider_extensions,
        "entry_points",
        lambda group: (
            [BrokenAIProviderEntryPoint()] if group == "fastapi_infra.ai_providers" else []
        ),
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AIPlugin()])

    with pytest.raises(
        ValueError,
        match=(
            "fastapi_infra.ai_providers:custom provider is missing required "
            r"method\(s\): stream_chat, embed"
        ),
    ):
        await manager.startup()


@pytest.mark.asyncio
async def test_ai_plugin_health_probe_uses_provider_probe(monkeypatch):
    async def fake_health_check(self):
        return self_health_status()

    def self_health_status():
        from infra.core.health import HealthStatus

        return HealthStatus(
            name="openai",
            status=HealthState.HEALTHY,
            details={"provider": "openai", "model_count": 2},
        )

    monkeypatch.setattr(OpenAIProvider, "health_check", fake_health_check)
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "health_probe": True,
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AIPlugin()])

    await manager.startup()
    await manager.shutdown()

    status = manager.health.snapshot()["ai"]
    assert status.status is HealthState.HEALTHY
    assert status.details["providers"]["openai"]["status"] == "healthy"
    assert status.details["providers"]["openai"]["details"] == {
        "provider": "openai",
        "model_count": 2,
    }


@pytest.mark.asyncio
async def test_ai_plugin_health_probe_checks_non_default_external_providers(monkeypatch):
    from infra.core.health import HealthStatus

    async def fake_health_check(self):
        return HealthStatus(
            name="openai",
            status=HealthState.UNHEALTHY,
            message="openai unavailable",
            details={"provider": "openai"},
        )

    monkeypatch.setattr(OpenAIProvider, "health_check", fake_health_check)
    settings = InfraSettings(
        infra={
            "plugins": {
                "ai": {
                    "enabled": True,
                    "config": {
                        "default_provider": "mock",
                        "providers": {"openai": {}},
                        "health_probe": True,
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[AIPlugin()])

    with pytest.raises(Exception, match="plugin is unhealthy: ai"):
        await manager.startup()

    status = manager.health.snapshot()["ai"]
    assert status.status is HealthState.UNHEALTHY
    assert status.message == "openai unavailable"
    assert status.details["providers"]["openai"]["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_ai_plugin_shutdown_closes_registry(monkeypatch):
    closed = False
    original_aclose = AIRegistry.aclose

    async def tracked_aclose(self):
        nonlocal closed
        closed = True
        await original_aclose(self)

    monkeypatch.setattr(AIRegistry, "aclose", tracked_aclose)
    settings = InfraSettings(infra={"plugins": {"ai": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[AIPlugin()])

    await manager.startup()
    await manager.shutdown()

    assert closed is True
