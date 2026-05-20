import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from pydantic import ValidationError

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.manager import PluginManager
from infra.plugins.speech import (
    OpenAISpeechError,
    OpenAISpeechProvider,
    OpenAISpeechProviderConfig,
    SpeechPlugin,
    SpeechService,
    SpeechSynthesisRequest,
    TranscriptionRequest,
)


class FakeOpenAITransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, url, headers, data):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "data": data,
            }
        )
        return self.responses.pop(0)


def test_urllib_openai_speech_transport_uses_configured_timeout(monkeypatch):
    import infra.plugins.speech.providers.openai as openai_speech_module

    calls = []

    class FakeHTTPResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        calls.append({"request": request, "timeout": timeout})
        return FakeHTTPResponse()

    monkeypatch.setattr(openai_speech_module.urllib.request, "urlopen", fake_urlopen)
    transport = openai_speech_module.UrllibOpenAISpeechTransport(timeout=11.5)

    status, body, headers = transport._request(
        "POST",
        "https://api.openai.test/v1/audio/speech",
        {},
        b"{}",
    )

    assert status == 200
    assert body == b"{}"
    assert headers["Content-Type"] == "application/json"
    assert calls[0]["timeout"] == 11.5


def test_openai_speech_provider_config_accepts_positive_timeout():
    config = OpenAISpeechProviderConfig.model_validate(
        {
            "api_key": "sk-test",
            "api_base": "https://api.openai.test",
            "timeout": 14.0,
            "max_attempts": 2,
            "retry_base_delay": 0,
        }
    )

    assert config.timeout == 14.0
    assert config.max_attempts == 2
    assert config.retry_base_delay == 0


def test_openai_speech_provider_config_rejects_invalid_retry_options():
    with pytest.raises(ValidationError, match="max_attempts"):
        OpenAISpeechProviderConfig.model_validate({"api_key": "sk-test", "max_attempts": 0})
    with pytest.raises(ValidationError, match="max_attempts"):
        OpenAISpeechProviderConfig.model_validate({"api_key": "sk-test", "max_attempts": True})
    with pytest.raises(ValidationError, match="retry_base_delay"):
        OpenAISpeechProviderConfig.model_validate({"api_key": "sk-test", "retry_base_delay": -0.1})
    with pytest.raises(ValidationError, match="timeout"):
        OpenAISpeechProviderConfig.model_validate({"api_key": "sk-test", "timeout": True})


def test_openai_speech_provider_config_rejects_blank_api_key():
    with pytest.raises(ValidationError, match="api_key"):
        OpenAISpeechProviderConfig.model_validate({"api_key": "   "})


def test_openai_speech_provider_config_rejects_invalid_response_formats():
    with pytest.raises(ValidationError, match="asr_response_format"):
        OpenAISpeechProviderConfig.model_validate(
            {"api_key": "sk-test", "asr_response_format": "xml"}
        )
    with pytest.raises(ValidationError, match="tts_response_format"):
        OpenAISpeechProviderConfig.model_validate(
            {"api_key": "sk-test", "tts_response_format": "wav"}
        )


def test_openai_speech_provider_config_rejects_invalid_api_base():
    with pytest.raises(ValidationError, match="api_base"):
        OpenAISpeechProviderConfig.model_validate(
            {"api_key": "sk-test", "api_base": "api.openai.test"}
        )


@pytest.mark.asyncio
async def test_openai_speech_health_check_probes_configured_models():
    transport = FakeOpenAITransport(
        [
            (200, json.dumps({"id": "gpt-4o-transcribe"}).encode(), {}),
            (200, json.dumps({"id": "gpt-4o-mini-tts"}).encode(), {}),
        ]
    )
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key="sk-test",
            api_base="https://api.openai.test",
            asr_model="gpt-4o-transcribe",
            tts_model="gpt-4o-mini-tts",
        ),
        transport=transport,
    )

    status = await provider.health_check()

    assert status.status is HealthState.HEALTHY
    assert status.details == {
        "provider": "openai",
        "asr_model": "gpt-4o-transcribe",
        "tts_model": "gpt-4o-mini-tts",
    }
    assert [request["method"] for request in transport.requests] == ["GET", "GET"]
    assert [request["url"] for request in transport.requests] == [
        "https://api.openai.test/v1/models/gpt-4o-transcribe",
        "https://api.openai.test/v1/models/gpt-4o-mini-tts",
    ]


@pytest.mark.asyncio
async def test_openai_speech_health_check_reports_upstream_failure():
    transport = FakeOpenAITransport(
        [
            (
                404,
                json.dumps({"error": {"message": "model not found"}}).encode(),
                {},
            )
        ]
    )
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(api_key="sk-test", api_base="https://api.openai.test"),
        transport=transport,
    )

    status = await provider.health_check()

    assert status.status is HealthState.UNHEALTHY
    assert status.message == "model not found"
    assert status.details == {"provider": "openai"}


@pytest.mark.asyncio
async def test_openai_transcribe_constructs_real_multipart_request():
    transport = FakeOpenAITransport(
        [(200, json.dumps({"text": "hello world"}).encode(), {"Content-Type": "application/json"})]
    )
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key="sk-test",
            api_base="https://api.openai.test",
            asr_model="gpt-4o-transcribe",
        ),
        transport=transport,
    )

    result = await provider.transcribe(
        TranscriptionRequest(
            audio=b"RIFFaudio",
            format="wav",
            language="en",
            prompt="domain words",
        )
    )

    request = transport.requests[0]
    body = request["data"]

    assert request["method"] == "POST"
    assert request["url"] == "https://api.openai.test/v1/audio/transcriptions"
    assert request["headers"]["Authorization"] == "Bearer sk-test"
    assert request["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert body.count(b'Content-Disposition: form-data; name="model"') == 1
    assert b'name="model"\r\n\r\ngpt-4o-transcribe' in body
    assert b'name="language"\r\n\r\nen' in body
    assert b'name="prompt"\r\n\r\ndomain words' in body
    assert b'name="response_format"\r\n\r\njson' in body
    assert b'name="file"; filename="audio.wav"' in body
    assert b"Content-Type: audio/x-wav" in body
    assert b"RIFFaudio" in body
    assert result.text == "hello world"
    assert result.provider == "openai"
    assert result.model == "gpt-4o-transcribe"


@pytest.mark.asyncio
async def test_openai_transcribe_supports_text_response():
    transport = FakeOpenAITransport([(200, b"plain transcript", {"Content-Type": "text/plain"})])
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key="sk-test",
            api_base="https://api.openai.test",
            asr_response_format="text",
        ),
        transport=transport,
    )

    result = await provider.transcribe(TranscriptionRequest(audio=b"audio", format="mp3"))

    request = transport.requests[0]
    assert b'name="response_format"\r\n\r\ntext' in request["data"]
    assert b'name="file"; filename="audio.mp3"' in request["data"]
    assert b"Content-Type: audio/mpeg" in request["data"]
    assert result.text == "plain transcript"


@pytest.mark.asyncio
async def test_openai_synthesize_constructs_real_json_request():
    transport = FakeOpenAITransport([(200, b"audio-bytes", {"Content-Type": "audio/mpeg"})])
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key="sk-test",
            api_base="https://api.openai.test",
            tts_model="gpt-4o-mini-tts",
            voice="verse",
            tts_response_format="mp3",
        ),
        transport=transport,
    )

    result = await provider.synthesize(SpeechSynthesisRequest(text="Say hi"))

    request = transport.requests[0]
    payload = json.loads(request["data"].decode())

    assert request["method"] == "POST"
    assert request["url"] == "https://api.openai.test/v1/audio/speech"
    assert request["headers"]["Authorization"] == "Bearer sk-test"
    assert request["headers"]["Content-Type"] == "application/json"
    assert payload == {
        "model": "gpt-4o-mini-tts",
        "input": "Say hi",
        "voice": "verse",
        "response_format": "mp3",
    }
    assert result.audio == b"audio-bytes"
    assert result.content_type == "audio/mpeg"
    assert result.provider == "openai"
    assert result.model == "gpt-4o-mini-tts"
    assert result.format == "mp3"


@pytest.mark.asyncio
async def test_openai_speech_retries_retryable_status_before_succeeding():
    transport = FakeOpenAITransport(
        [
            (
                429,
                json.dumps({"error": {"message": "rate limited"}}).encode(),
                {"Content-Type": "application/json"},
            ),
            (200, b"audio-bytes", {"Content-Type": "audio/mpeg"}),
        ]
    )
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key="sk-test",
            api_base="https://api.openai.test",
            max_attempts=2,
            retry_base_delay=0,
        ),
        transport=transport,
    )

    result = await provider.synthesize(SpeechSynthesisRequest(text="Say hi"))

    assert result.audio == b"audio-bytes"
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_openai_speech_retry_uses_retry_after_header(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("infra.plugins.retry.asyncio.sleep", fake_sleep)
    transport = FakeOpenAITransport(
        [
            (
                429,
                json.dumps({"error": {"message": "rate limited"}}).encode(),
                {"Content-Type": "application/json", "Retry-After": "2"},
            ),
            (200, b"audio-bytes", {"Content-Type": "audio/mpeg"}),
        ]
    )
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key="sk-test",
            api_base="https://api.openai.test",
            max_attempts=2,
            retry_base_delay=10,
        ),
        transport=transport,
    )

    result = await provider.synthesize(SpeechSynthesisRequest(text="Say hi"))

    assert result.audio == b"audio-bytes"
    assert sleeps == [2]


@pytest.mark.asyncio
async def test_openai_speech_retry_uses_retry_after_http_date_header(monkeypatch):
    import infra.plugins.speech.providers.openai as openai_speech_module

    sleeps: list[float] = []
    now = datetime(2026, 5, 12, 8, 0, tzinfo=UTC)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("infra.plugins.retry.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(openai_speech_module, "_utc_now", lambda: now)
    transport = FakeOpenAITransport(
        [
            (
                429,
                json.dumps({"error": {"message": "rate limited"}}).encode(),
                {
                    "Content-Type": "application/json",
                    "Retry-After": format_datetime(now + timedelta(seconds=3)),
                },
            ),
            (200, b"audio-bytes", {"Content-Type": "audio/mpeg"}),
        ]
    )
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key="sk-test",
            api_base="https://api.openai.test",
            max_attempts=2,
            retry_base_delay=10,
        ),
        transport=transport,
    )

    result = await provider.synthesize(SpeechSynthesisRequest(text="Say hi"))

    assert result.audio == b"audio-bytes"
    assert sleeps == [3]


@pytest.mark.asyncio
async def test_openai_speech_does_not_retry_non_retryable_api_errors():
    transport = FakeOpenAITransport(
        [
            (
                401,
                json.dumps({"error": {"message": "invalid api key"}}).encode(),
                {"Content-Type": "application/json"},
            ),
            (200, b"unexpected", {"Content-Type": "audio/mpeg"}),
        ]
    )
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key="sk-test",
            api_base="https://api.openai.test",
            max_attempts=2,
            retry_base_delay=0,
        ),
        transport=transport,
    )

    with pytest.raises(OpenAISpeechError) as exc:
        await provider.synthesize(SpeechSynthesisRequest(text="Say hi"))

    assert exc.value.status_code == 401
    assert exc.value.retryable is False
    assert str(exc.value) == "invalid api key"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_openai_error_response_raises_message():
    transport = FakeOpenAITransport(
        [
            (
                401,
                json.dumps({"error": {"message": "invalid api key"}}).encode(),
                {"Content-Type": "application/json"},
            )
        ]
    )
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(api_key="sk-test", api_base="https://api.openai.test"),
        transport=transport,
    )

    with pytest.raises(RuntimeError, match="invalid api key"):
        await provider.synthesize(SpeechSynthesisRequest(text="Say hi"))


@pytest.mark.asyncio
async def test_speech_plugin_registers_openai_provider_from_config():
    settings = InfraSettings(
        infra={
            "plugins": {
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "providers": {
                            "openai": {
                                "api_key": "sk-test",
                                "api_base": "https://api.openai.test",
                            }
                        },
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
    assert service.registry.names() == ["openai"]


@pytest.mark.asyncio
async def test_speech_plugin_fails_when_openai_enabled_without_api_key():
    settings = InfraSettings(
        infra={
            "plugins": {
                "speech": {
                    "enabled": True,
                    "config": {
                        "default_provider": "openai",
                        "providers": {"openai": {}},
                    },
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[SpeechPlugin()])

    with pytest.raises(ValidationError, match="api_key"):
        await manager.startup()
