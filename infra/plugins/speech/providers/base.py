from typing import Protocol

from infra.plugins.speech.models import (
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
)


class SpeechProvider(Protocol):
    @property
    def name(self) -> str:
        raise NotImplementedError

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        raise NotImplementedError

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
    ) -> SpeechSynthesisResult:
        raise NotImplementedError
