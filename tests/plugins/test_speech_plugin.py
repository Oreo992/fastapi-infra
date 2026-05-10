import pytest

from infra.config.models import InfraSettings
from infra.plugins.manager import PluginManager
from infra.plugins.speech import (
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
