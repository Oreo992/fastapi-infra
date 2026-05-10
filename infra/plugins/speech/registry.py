from infra.plugins.speech.models import (
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
)
from infra.plugins.speech.providers.base import SpeechProvider
from infra.plugins.speech.providers.mock import MockSpeechProvider


class SpeechProviderRegistry:
    def __init__(self, default_provider: str = "mock") -> None:
        self.default_provider = default_provider
        self._providers: dict[str, SpeechProvider] = {}

    @classmethod
    def with_mock(cls) -> "SpeechProviderRegistry":
        registry = cls(default_provider="mock")
        registry.register(MockSpeechProvider(), default=True)
        return registry

    def register(self, provider: SpeechProvider, *, default: bool = False) -> None:
        self._providers[provider.name] = provider
        if default:
            self.default_provider = provider.name

    def get(self, name: str | None = None) -> SpeechProvider:
        provider_name = name or self.default_provider
        provider = self._providers.get(provider_name)
        if provider is None:
            raise LookupError(f"unknown speech provider: {provider_name}")
        return provider

    async def transcribe(
        self,
        request: TranscriptionRequest,
        *,
        provider: str | None = None,
    ) -> TranscriptionResult:
        return await self.get(provider).transcribe(request)

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
        *,
        provider: str | None = None,
    ) -> SpeechSynthesisResult:
        return await self.get(provider).synthesize(request)

    def names(self) -> list[str]:
        return sorted(self._providers)
