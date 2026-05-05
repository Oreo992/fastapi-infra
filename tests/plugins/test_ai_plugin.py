import pytest
from pydantic import ValidationError

from infra.config.models import InfraSettings
from infra.plugins.ai import AIPlugin
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

    with pytest.raises(ValidationError):
        await manager.startup()
