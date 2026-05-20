from pydantic import BaseModel


class TranscriptionRequest(BaseModel):
    audio: bytes
    format: str = "wav"
    language: str | None = None
    prompt: str | None = None
    model: str = "mock-asr"


class TranscriptionResult(BaseModel):
    text: str
    language: str | None = None
    provider: str
    model: str


class SpeechSynthesisRequest(BaseModel):
    text: str
    voice: str = "default"
    format: str = "wav"
    model: str = "mock-tts"


class SpeechSynthesisResult(BaseModel):
    audio: bytes
    content_type: str
    provider: str
    model: str
    format: str | None = None
