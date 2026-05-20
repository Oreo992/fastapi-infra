import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.manager import PluginManager
from infra.plugins.speech import (
    OpenAISpeechProvider,
    SpeechPlugin,
    SpeechProviderRegistry,
    SpeechService,
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
)


@pytest.mark.asyncio
async def test_speech_service_mock_transcribes_and_synthesizes():
    service = SpeechService(SpeechProviderRegistry.with_mock())

    transcription = await service.transcribe(b"audio")
    synthesis = await service.synthesize("hello")

    assert transcription == TranscriptionResult(
        text="mock transcription",
        language=None,
        provider="mock",
        model="mock-asr",
    )
    assert synthesis == SpeechSynthesisResult(
        audio=b"mock-tts:hello",
        content_type="audio/wav",
        provider="mock",
        model="mock-tts",
    )


@pytest.mark.asyncio
async def test_speech_service_supports_provider_override_with_fake_provider():
    class FakeSpeechProvider:
        name = "fake"

        async def transcribe(
            self,
            request: TranscriptionRequest,
        ) -> TranscriptionResult:
            return TranscriptionResult(
                text=f"fake {request.format}",
                language=request.language,
                provider=self.name,
                model=request.model,
            )

        async def synthesize(
            self,
            request: SpeechSynthesisRequest,
        ) -> SpeechSynthesisResult:
            return SpeechSynthesisResult(
                audio=f"fake:{request.text}:{request.voice}".encode(),
                content_type=f"audio/{request.format}",
                provider=self.name,
                model=request.model,
            )

    registry = SpeechProviderRegistry.with_mock()
    registry.register(FakeSpeechProvider())
    service = SpeechService(registry)

    transcription = await service.transcribe(
        b"audio",
        format="mp3",
        language="en",
        provider="fake",
    )
    synthesis = await service.synthesize("hello", voice="amy", provider="fake")

    assert transcription.text == "fake mp3"
    assert transcription.language == "en"
    assert transcription.provider == "fake"
    assert synthesis.audio == b"fake:hello:amy"
    assert synthesis.content_type == "audio/wav"
    assert synthesis.provider == "fake"


@pytest.mark.asyncio
async def test_speech_plugin_registers_service_with_plugin_manager():
    settings = InfraSettings(infra={"plugins": {"speech": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[SpeechPlugin()])

    await manager.startup()
    service = manager.get("speech")
    result = await service.transcribe(b"audio")
    await manager.shutdown()

    assert isinstance(service, SpeechService)
    assert result.text == "mock transcription"
    assert result.provider == "mock"


@pytest.mark.asyncio
async def test_speech_plugin_reports_external_provider_as_degraded_until_live_checked():
    settings = InfraSettings(
        infra={
            "plugins": {
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "providers": {"openai": {"api_key": "sk-test"}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[SpeechPlugin()])

    await manager.startup()
    service = manager.get("speech")
    await manager.shutdown()

    assert isinstance(service, SpeechService)
    assert manager.health.snapshot()["speech"].status is HealthState.DEGRADED


@pytest.mark.asyncio
async def test_speech_plugin_health_probe_checks_non_default_external_providers(monkeypatch):
    from infra.core.health import HealthStatus

    async def fake_health_check(self):
        return HealthStatus(
            name="openai",
            status=HealthState.UNHEALTHY,
            message="openai speech unavailable",
            details={"provider": "openai"},
        )

    monkeypatch.setattr(OpenAISpeechProvider, "health_check", fake_health_check)
    settings = InfraSettings(
        infra={
            "plugins": {
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "mock",
                        "providers": {"openai": {"api_key": "sk-test"}},
                        "health_probe": True,
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[SpeechPlugin()])

    with pytest.raises(Exception, match="plugin is unhealthy: speech"):
        await manager.startup()

    status = manager.health.snapshot()["speech"]
    assert status.status is HealthState.UNHEALTHY
    assert status.message == "openai speech unavailable"
    assert status.details["providers"]["openai"]["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_speech_plugin_rejects_unknown_provider_at_startup():
    settings = InfraSettings(
        infra={
            "plugins": {
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "unknown",
                        "providers": {"unknown": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[SpeechPlugin()])

    with pytest.raises(ValueError, match="unknown speech provider: unknown"):
        await manager.startup()


class CustomSpeechProvider:
    name = "custom"

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        return TranscriptionResult(
            text="custom transcription",
            language=request.language,
            provider=self.name,
            model=request.model,
        )

    async def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult:
        return SpeechSynthesisResult(
            audio=f"custom:{request.text}".encode(),
            content_type="audio/wav",
            provider=self.name,
            model=request.model,
        )


class FakeProviderEntryPoint:
    name = "custom"

    def load(self):
        return lambda config: CustomSpeechProvider()


class BrokenSpeechProvider:
    name = "custom"

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        return TranscriptionResult(
            text="broken transcription",
            language=request.language,
            provider=self.name,
            model=request.model,
        )


class BrokenProviderEntryPoint:
    name = "custom"

    def load(self):
        return lambda config: BrokenSpeechProvider()


@pytest.mark.asyncio
async def test_speech_plugin_registers_custom_entry_point_provider(monkeypatch):
    import infra.plugins.provider_extensions as provider_extensions

    monkeypatch.setattr(
        provider_extensions,
        "entry_points",
        lambda group: (
            [FakeProviderEntryPoint()] if group == "fastapi_infra.speech_providers" else []
        ),
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[SpeechPlugin()])

    await manager.startup()
    service = manager.get("speech")
    result = await service.transcribe(b"audio")

    assert result.text == "custom transcription"
    assert result.provider == "custom"
    assert manager.health.snapshot()["speech"].status is HealthState.DEGRADED


@pytest.mark.asyncio
async def test_speech_plugin_rejects_custom_provider_missing_required_methods(monkeypatch):
    import infra.plugins.provider_extensions as provider_extensions

    monkeypatch.setattr(
        provider_extensions,
        "entry_points",
        lambda group: (
            [BrokenProviderEntryPoint()] if group == "fastapi_infra.speech_providers" else []
        ),
    )
    settings = InfraSettings(
        infra={
            "plugins": {
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "custom",
                        "providers": {"custom": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[SpeechPlugin()])

    with pytest.raises(
        ValueError,
        match=(
            "fastapi_infra.speech_providers:custom provider is missing required "
            r"method\(s\): synthesize"
        ),
    ):
        await manager.startup()
