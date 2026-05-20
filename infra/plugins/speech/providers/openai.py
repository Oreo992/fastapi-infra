import asyncio
import json
import mimetypes
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import ClassVar, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from infra.core.health import HealthState, HealthStatus
from infra.plugins.retry import retry_provider_operation
from infra.plugins.speech.models import (
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
)

ASR_RESPONSE_FORMATS = frozenset({"json", "text", "srt", "verbose_json", "vtt"})
TTS_RESPONSE_FORMATS = frozenset({"mp3", "opus", "aac", "flac", "pcm"})


class OpenAISpeechTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> tuple[int, bytes, dict[str, str]]:
        raise NotImplementedError


class UrllibOpenAISpeechTransport:
    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> tuple[int, bytes, dict[str, str]]:
        return await asyncio.to_thread(self._request, method, url, headers, data)

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)
        except urllib.error.URLError as exc:
            raise OpenAISpeechError(
                f"openai speech transport error: {exc}", retryable=True
            ) from exc


class OpenAISpeechProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    DEFAULT_API_BASE: ClassVar[str] = "https://api.openai.com"
    DEFAULT_ASR_MODEL: ClassVar[str] = "gpt-4o-mini-transcribe"
    DEFAULT_TTS_MODEL: ClassVar[str] = "gpt-4o-mini-tts"
    DEFAULT_VOICE: ClassVar[str] = "alloy"

    api_key: str = Field(min_length=1, repr=False)
    api_base: str = DEFAULT_API_BASE
    asr_model: str = Field(default=DEFAULT_ASR_MODEL, min_length=1)
    tts_model: str = Field(default=DEFAULT_TTS_MODEL, min_length=1)
    voice: str = Field(default=DEFAULT_VOICE, min_length=1)
    asr_response_format: str = "json"
    tts_response_format: str = "mp3"
    timeout: float = Field(default=60.0, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    retry_base_delay: float = Field(default=0.25, ge=0)

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("api_base must be an absolute http(s) URL")
        return value.rstrip("/")

    @field_validator("asr_response_format")
    @classmethod
    def validate_asr_response_format(cls, value: str) -> str:
        if value not in ASR_RESPONSE_FORMATS:
            raise ValueError(
                "asr_response_format must be one of: " + ", ".join(sorted(ASR_RESPONSE_FORMATS))
            )
        return value

    @field_validator("tts_response_format")
    @classmethod
    def validate_tts_response_format(cls, value: str) -> str:
        if value not in TTS_RESPONSE_FORMATS:
            raise ValueError(
                "tts_response_format must be one of: " + ", ".join(sorted(TTS_RESPONSE_FORMATS))
            )
        return value

    @field_validator("timeout", "max_attempts", "retry_base_delay", mode="before")
    @classmethod
    def reject_bool_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value


class OpenAISpeechError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class OpenAISpeechProvider:
    name = "openai"
    retry_status_codes = frozenset({408, 409, 429, 500, 502, 503, 504})

    def __init__(
        self,
        config: OpenAISpeechProviderConfig,
        transport: OpenAISpeechTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibOpenAISpeechTransport(timeout=config.timeout)

    async def health_check(self) -> HealthStatus:
        details = {
            "provider": self.name,
            "asr_model": self.config.asr_model,
            "tts_model": self.config.tts_model,
        }
        try:
            await self._probe_model(self.config.asr_model)
            if self.config.tts_model != self.config.asr_model:
                await self._probe_model(self.config.tts_model)
        except Exception as exc:
            return HealthStatus(
                name=self.name,
                status=HealthState.UNHEALTHY,
                message=str(exc) or exc.__class__.__name__,
                details={"provider": self.name},
            )
        return HealthStatus(name=self.name, status=HealthState.HEALTHY, details=details)

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        model = self._asr_model(request)
        fields: dict[str, str] = {"model": model}
        if request.language is not None:
            fields["language"] = request.language
        if request.prompt is not None:
            fields["prompt"] = request.prompt

        response_format = self._transcription_response_format()
        if response_format is not None:
            fields["response_format"] = response_format

        filename = f"audio.{request.format.lstrip('.') or 'wav'}"
        content_type = (
            mimetypes.guess_type(filename)[0] or f"audio/{request.format.lstrip('.') or 'wav'}"
        )
        body, content_type_header = _encode_multipart(
            fields,
            file_field="file",
            filename=filename,
            file_content_type=content_type,
            file_content=request.audio,
        )
        status, response_body, _headers = await self._request(
            "POST",
            f"{self.config.api_base}/v1/audio/transcriptions",
            {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": content_type_header,
            },
            body,
        )
        if status >= 400:
            self._raise_for_status(status, response_body, "openai transcription failed")

        text = self._transcription_text(response_body)
        return TranscriptionResult(
            text=text,
            language=request.language,
            provider=self.name,
            model=model,
        )

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
    ) -> SpeechSynthesisResult:
        model = self._tts_model(request)
        response_format = self._speech_response_format(request)
        payload = {
            "model": model,
            "input": request.text,
            "voice": self._voice(request),
            "response_format": response_format,
        }
        body = json.dumps(payload).encode()
        status, response_body, headers = await self._request(
            "POST",
            f"{self.config.api_base}/v1/audio/speech",
            {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            body,
        )
        if status >= 400:
            self._raise_for_status(status, response_body, "openai speech synthesis failed")

        return SpeechSynthesisResult(
            audio=response_body,
            content_type=headers.get("Content-Type", f"audio/{response_format}"),
            provider=self.name,
            model=model,
            format=response_format,
        )

    async def _probe_model(self, model: str) -> None:
        status, response_body, _headers = await self._request(
            "GET",
            f"{self.config.api_base}/v1/models/{urllib.parse.quote(model, safe='')}",
            {"Authorization": f"Bearer {self.config.api_key}"},
            None,
        )
        if status >= 400:
            self._raise_for_status(status, response_body, "openai model probe failed")

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> tuple[int, bytes, dict[str, str]]:
        async def request_once() -> tuple[int, bytes, dict[str, str]]:
            try:
                return await self.transport.request(
                    method,
                    url,
                    headers,
                    data,
                )
            except OpenAISpeechError:
                raise
            except Exception as exc:
                raise OpenAISpeechError(
                    f"openai speech transport error: {exc}",
                    retryable=True,
                ) from exc

        return await retry_provider_operation(
            request_once,
            max_attempts=self.config.max_attempts,
            base_delay=self.config.retry_base_delay,
            is_retryable_exception=lambda exc: isinstance(exc, OpenAISpeechError) and exc.retryable,
            is_retryable_result=lambda response: response[0] in self.retry_status_codes,
            retry_delay_for_result=lambda response: _retry_after_delay(response[2]),
            exhausted_message="openai speech max_attempts must allow at least one request",
        )

    def _asr_model(self, request: TranscriptionRequest) -> str:
        if request.model and request.model != "mock-asr":
            return request.model
        return self.config.asr_model

    def _tts_model(self, request: SpeechSynthesisRequest) -> str:
        if request.model and request.model != "mock-tts":
            return request.model
        return self.config.tts_model

    def _voice(self, request: SpeechSynthesisRequest) -> str:
        if request.voice and request.voice != "default":
            return request.voice
        return self.config.voice

    def _speech_response_format(self, request: SpeechSynthesisRequest) -> str:
        if request.format and request.format != "wav":
            return request.format
        return self.config.tts_response_format

    def _transcription_response_format(self) -> str | None:
        if self.config.asr_response_format in ASR_RESPONSE_FORMATS:
            return self.config.asr_response_format
        return "json"

    def _transcription_text(self, response_body: bytes) -> str:
        decoded = response_body.decode()
        try:
            payload = json.loads(decoded or "{}")
        except json.JSONDecodeError:
            return decoded

        if not isinstance(payload, dict):
            raise RuntimeError("openai transcription returned an unexpected response")
        text = payload.get("text")
        if not isinstance(text, str):
            raise RuntimeError("openai transcription response missing text")
        return text

    def _error_message(self, response_body: bytes, fallback: str) -> str:
        try:
            payload = json.loads(response_body.decode() or "{}")
        except json.JSONDecodeError:
            return fallback
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str):
                    return message
        return fallback

    def _raise_for_status(self, status: int, response_body: bytes, fallback: str) -> None:
        raise OpenAISpeechError(
            self._error_message(response_body, fallback),
            status_code=status,
            retryable=status in self.retry_status_codes,
        )


def _encode_multipart(
    fields: Mapping[str, str],
    *,
    file_field: str,
    filename: str,
    file_content_type: str,
    file_content: bytes,
) -> tuple[bytes, str]:
    boundary = f"----fastapi-infra-{secrets.token_hex(16)}"
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.extend(
            [
                f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="{_quote(name)}"'.encode(),
                b"",
                value.encode(),
            ]
        )
    lines.extend(
        [
            f"--{boundary}".encode(),
            (
                f'Content-Disposition: form-data; name="{_quote(file_field)}"; '
                f'filename="{_quote(filename)}"'
            ).encode(),
            f"Content-Type: {file_content_type}".encode(),
            b"",
            file_content,
            f"--{boundary}--".encode(),
            b"",
        ]
    )
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


def _retry_after_delay(headers: Mapping[str, str]) -> float | None:
    value = _header_value(headers, "Retry-After")
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = (retry_at - _utc_now()).total_seconds()
    return delay if delay >= 0 else None


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")
