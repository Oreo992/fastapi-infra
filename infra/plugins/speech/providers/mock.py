from infra.plugins.speech.models import (
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
)


class MockSpeechProvider:
    name = "mock"

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        return TranscriptionResult(
            text="mock transcription",
            language=request.language,
            provider=self.name,
            model=request.model,
        )

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
    ) -> SpeechSynthesisResult:
        return SpeechSynthesisResult(
            audio=f"mock-tts:{request.text}".encode(),
            content_type=f"audio/{request.format}",
            provider=self.name,
            model=request.model,
        )
