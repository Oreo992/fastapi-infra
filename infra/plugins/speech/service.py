from infra.plugins.speech.models import (
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
)
from infra.plugins.speech.registry import SpeechProviderRegistry


class SpeechService:
    def __init__(self, registry: SpeechProviderRegistry) -> None:
        self.registry = registry

    async def transcribe(
        self,
        audio: bytes,
        *,
        format: str = "wav",
        language: str | None = None,
        model: str = "mock-asr",
        provider: str | None = None,
    ) -> TranscriptionResult:
        return await self.registry.transcribe(
            TranscriptionRequest(
                audio=audio,
                format=format,
                language=language,
                model=model,
            ),
            provider=provider,
        )

    async def synthesize(
        self,
        text: str,
        *,
        voice: str = "default",
        format: str = "wav",
        model: str = "mock-tts",
        provider: str | None = None,
    ) -> SpeechSynthesisResult:
        return await self.registry.synthesize(
            SpeechSynthesisRequest(
                text=text,
                voice=voice,
                format=format,
                model=model,
            ),
            provider=provider,
        )
