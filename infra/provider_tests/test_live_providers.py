import hashlib
import hmac
import importlib.util
import os
import struct
import time
import uuid
import wave
from contextlib import suppress
from io import BytesIO
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest

from infra.cache.service import CacheService
from infra.core.health import HealthState
from infra.database.manager import DatabaseManager
from infra.plugins.ai import ChatMessage, ChatRequest, EmbeddingRequest
from infra.plugins.ai.adapters.anthropic import AnthropicAIProvider
from infra.plugins.ai.adapters.gemini import GeminiAIProvider
from infra.plugins.ai.adapters.openai import OpenAIProvider
from infra.plugins.ai.plugin import AIProviderConfig
from infra.plugins.notifications import SMTPNotificationConfig, SMTPNotificationService
from infra.plugins.payment import (
    PaymentProviderRegistry,
    PaymentService,
    SqlPaymentStore,
    StripePaymentProvider,
    StripeProviderConfig,
    verify_webhook_signature,
)
from infra.plugins.speech.models import SpeechSynthesisRequest, TranscriptionRequest
from infra.plugins.speech.providers.openai import (
    OpenAISpeechProvider,
    OpenAISpeechProviderConfig,
)
from infra.plugins.storage import S3Storage, S3StorageConfig

pytestmark = pytest.mark.integration


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


def _require_env(*names: str) -> dict[str, str]:
    values = {name: _env(name) for name in names}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        pytest.skip(f"live provider test requires env vars: {', '.join(missing)}")
    return {name: value for name, value in values.items() if value is not None}


def _require_module(module_name: str, install_name: str) -> None:
    try:
        available = importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        available = False
    if not available:
        pytest.skip(f"live provider test requires installed package: {install_name}")


def _assert_exact_live_chat_response(content: str) -> None:
    assert content.strip().lower().rstrip(".") == "fastapi-infra-live"


def _live_mysql_manager() -> DatabaseManager:
    _require_module("aiomysql", "aiomysql")
    env = _require_env(
        "MYSQL_LIVE_HOST",
        "MYSQL_LIVE_USER",
        "MYSQL_LIVE_PASSWORD",
        "MYSQL_LIVE_DB",
    )
    return DatabaseManager(
        {
            "mysql_enabled": True,
            "redis_enabled": False,
            "mysql_host": env["MYSQL_LIVE_HOST"],
            "mysql_port": int(_env("MYSQL_LIVE_PORT") or "3306"),
            "mysql_user": env["MYSQL_LIVE_USER"],
            "mysql_password": env["MYSQL_LIVE_PASSWORD"],
            "mysql_db": env["MYSQL_LIVE_DB"],
            "mysql_pool_minsize": 1,
            "mysql_pool_maxsize": 1,
            "mysql_connect_timeout": int(_env("MYSQL_LIVE_CONNECT_TIMEOUT") or "5"),
        }
    )


def _live_stripe_provider() -> StripePaymentProvider:
    env = _require_env("STRIPE_API_KEY")
    return StripePaymentProvider(
        StripeProviderConfig(
            api_key=env["STRIPE_API_KEY"],
            api_base=_env("STRIPE_API_BASE") or StripeProviderConfig.DEFAULT_API_BASE,
            timeout=float(_env("STRIPE_LIVE_TIMEOUT") or "30"),
        )
    )


@pytest.mark.asyncio
async def test_live_mysql_database_manager_round_trip():
    table = f"fastapi_infra_live_{uuid.uuid4().hex}"
    manager = _live_mysql_manager()

    await manager.initialize()
    try:
        assert await manager.health_check() is True
        await manager.execute_sql(
            f"CREATE TABLE {table} (id VARCHAR(64) PRIMARY KEY, payload VARCHAR(255))"
        )
        await manager.execute_sql(
            f"INSERT INTO {table} (id, payload) VALUES (%s, %s)",
            ("row-1", "fastapi-infra-live"),
        )
        rows = await manager.fetch_all(
            f"SELECT id, payload FROM {table} WHERE id = %s",
            ("row-1",),
        )
        assert rows == [{"id": "row-1", "payload": "fastapi-infra-live"}]
    finally:
        with suppress(Exception):
            await manager.execute_sql(f"DROP TABLE IF EXISTS {table}")
        await manager.close()


@pytest.mark.asyncio
async def test_live_redis_cache_service_round_trip():
    _require_module("redis", "redis")
    env = _require_env("REDIS_LIVE_URL")
    manager = DatabaseManager(
        {
            "mysql_enabled": False,
            "redis_enabled": True,
            "redis_url": env["REDIS_LIVE_URL"],
            "redis_socket_connect_timeout": int(_env("REDIS_LIVE_CONNECT_TIMEOUT") or "5"),
        }
    )
    cache = CacheService(
        namespace=f"fastapi-infra-live:{uuid.uuid4().hex}",
        db_manager=manager,
    )

    await manager.initialize()
    try:
        assert await manager.health_check() is True
        assert await cache.set("sample", {"value": "ok"}, ttl=60) is True
        assert await cache.get("sample") == {"value": "ok"}
        assert await cache.exists("sample") is True
        assert await cache.delete("sample") is True
        assert await cache.get("sample") is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_live_stripe_checkout_session_creation():
    provider = _live_stripe_provider()
    reference = f"fastapi-infra-live-{uuid.uuid4().hex}"

    health = await provider.health_check()
    assert health.status is HealthState.HEALTHY
    assert health.details["provider"] == "stripe"

    checkout = await provider.create_checkout(
        amount=100,
        currency="usd",
        reference=reference,
        success_url="https://example.com/fastapi-infra/success",
        cancel_url="https://example.com/fastapi-infra/cancel",
        metadata={"test": "fastapi-infra-live"},
        provider_options={"idempotency_key": reference},
    )

    assert checkout.id.startswith("cs_")
    assert checkout.amount == 100
    assert checkout.currency == "USD"
    assert checkout.reference == reference
    assert checkout.url.startswith("https://")

    fetched = await provider.get_checkout(checkout.id)
    assert fetched.id == checkout.id
    assert fetched.amount == checkout.amount
    assert fetched.currency == checkout.currency


@pytest.mark.asyncio
async def test_live_stripe_checkout_persists_to_mysql_store():
    manager = _live_mysql_manager()
    provider = _live_stripe_provider()
    checkout_table = f"fastapi_infra_checkout_{uuid.uuid4().hex}"
    refund_table = f"fastapi_infra_refund_{uuid.uuid4().hex}"
    store = SqlPaymentStore(
        manager,
        checkout_table=checkout_table,
        refund_table=refund_table,
    )
    registry = PaymentProviderRegistry(default_provider="stripe")
    registry.register(provider, default=True)
    service = PaymentService(registry, store=store)
    reference = f"fastapi-infra-live-store-{uuid.uuid4().hex}"

    await manager.initialize()
    try:
        checkout = await service.create_checkout(
            amount=100,
            currency="usd",
            reference=reference,
            success_url="https://example.com/fastapi-infra/success",
            cancel_url="https://example.com/fastapi-infra/cancel",
            metadata={"test": "fastapi-infra-live-store"},
            provider_options={"idempotency_key": reference},
        )
        rows = await manager.fetch_all(
            f"""
            SELECT provider, checkout_id, amount, currency, reference, status, url
            FROM {checkout_table}
            WHERE provider = %s AND checkout_id = %s
            """,
            ("stripe", checkout.id),
        )

        assert rows == [
            {
                "provider": "stripe",
                "checkout_id": checkout.id,
                "amount": checkout.amount,
                "currency": checkout.currency,
                "reference": reference,
                "status": checkout.status,
                "url": checkout.url,
            }
        ]
    finally:
        with suppress(Exception):
            await manager.execute_sql(f"DROP TABLE IF EXISTS {checkout_table}")
        with suppress(Exception):
            await manager.execute_sql(f"DROP TABLE IF EXISTS {refund_table}")
        await manager.close()


def test_live_stripe_webhook_signature_entrypoint():
    env = _require_env("STRIPE_WEBHOOK_SECRET")
    payload = b'{"id":"evt_fastapi_infra_live","type":"checkout.session.completed"}'
    timestamp = str(int(time.time()))
    signed_payload = f"{timestamp}.".encode() + payload
    signature = hmac.new(
        env["STRIPE_WEBHOOK_SECRET"].encode(),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    header = f"t={timestamp},v1={signature}"

    assert verify_webhook_signature(payload, header, env["STRIPE_WEBHOOK_SECRET"])


def _live_s3_storage() -> S3Storage:
    env = _require_env(
        "S3_LIVE_BUCKET",
        "S3_LIVE_REGION",
        "S3_LIVE_ACCESS_KEY_ID",
        "S3_LIVE_SECRET_ACCESS_KEY",
    )
    force_path_style = (_env("S3_LIVE_FORCE_PATH_STYLE") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    return S3Storage(
        S3StorageConfig(
            bucket=env["S3_LIVE_BUCKET"],
            region=env["S3_LIVE_REGION"],
            access_key_id=env["S3_LIVE_ACCESS_KEY_ID"],
            secret_access_key=env["S3_LIVE_SECRET_ACCESS_KEY"],
            endpoint_url=_env("S3_LIVE_ENDPOINT_URL"),
            force_path_style=force_path_style,
            timeout=float(_env("S3_LIVE_TIMEOUT") or "30"),
        )
    )


@pytest.mark.asyncio
async def test_live_s3_put_get_list_and_presign():
    storage = _live_s3_storage()
    prefix = _env("S3_LIVE_PREFIX") or "fastapi-infra-live/"
    key = f"{prefix.rstrip('/')}/{uuid.uuid4().hex}.txt"
    body = f"fastapi-infra live test {uuid.uuid4().hex}\n".encode()
    uploaded = False

    try:
        health = await storage.health_check()
        assert health.status is HealthState.HEALTHY
        assert health.details["provider"] == "s3"

        await storage.put_object(
            key,
            body,
            content_type="text/plain",
            metadata={"suite": "fastapi-infra"},
        )
        uploaded = True

        assert await storage.get_object(key) == body
        assert await storage.exists(key) is True
        assert key in await storage.list_objects(prefix.rstrip("/") + "/")

        presigned_url = storage.presign_get_url(key, expires_seconds=300)
        assert urlsplit(presigned_url).scheme in {"http", "https"}
        assert "X-Amz-Signature=" in presigned_url
        assert "X-Amz-Expires=300" in presigned_url
        with urlopen(presigned_url, timeout=10) as response:
            assert response.read() == body

        await storage.delete_object(key)
        uploaded = False
        assert await storage.exists(key) is False
    finally:
        if uploaded:
            with suppress(Exception):
                await storage.delete_object(key)


@pytest.mark.asyncio
async def test_live_openai_speech_transcription():
    env = _require_env("OPENAI_API_KEY")
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key=env["OPENAI_API_KEY"],
            api_base=_env("OPENAI_API_BASE") or OpenAISpeechProviderConfig.DEFAULT_API_BASE,
            asr_model=_env("OPENAI_ASR_MODEL") or OpenAISpeechProviderConfig.DEFAULT_ASR_MODEL,
            tts_model=_env("OPENAI_TTS_MODEL") or OpenAISpeechProviderConfig.DEFAULT_TTS_MODEL,
            timeout=float(_env("OPENAI_SPEECH_TIMEOUT") or "60"),
        )
    )

    health = await provider.health_check()
    assert health.status is HealthState.HEALTHY
    assert health.details["provider"] == "openai"

    result = await provider.transcribe(
        TranscriptionRequest(
            audio=_speech_sample_wav(),
            format="wav",
            language="en",
            prompt="The audio says fastapi infra live provider test.",
        )
    )

    assert result.provider == "openai"
    assert result.model
    assert isinstance(result.text, str)


@pytest.mark.asyncio
async def test_live_openai_speech_synthesis():
    env = _require_env("OPENAI_API_KEY")
    provider = OpenAISpeechProvider(
        OpenAISpeechProviderConfig(
            api_key=env["OPENAI_API_KEY"],
            api_base=_env("OPENAI_API_BASE") or OpenAISpeechProviderConfig.DEFAULT_API_BASE,
            asr_model=_env("OPENAI_ASR_MODEL") or OpenAISpeechProviderConfig.DEFAULT_ASR_MODEL,
            tts_model=_env("OPENAI_TTS_MODEL") or OpenAISpeechProviderConfig.DEFAULT_TTS_MODEL,
            voice=_env("OPENAI_VOICE") or OpenAISpeechProviderConfig.DEFAULT_VOICE,
            tts_response_format="mp3",
            timeout=float(_env("OPENAI_SPEECH_TIMEOUT") or "60"),
        )
    )

    health = await provider.health_check()
    assert health.status is HealthState.HEALTHY
    assert health.details["provider"] == "openai"

    result = await provider.synthesize(
        SpeechSynthesisRequest(
            text="fastapi infra live provider test",
            format="mp3",
        )
    )

    assert result.provider == "openai"
    assert result.audio
    assert result.content_type.startswith("audio/")


def _speech_sample_wav() -> bytes:
    sample_rate = 16_000
    duration_seconds = 1
    amplitude = 0
    frame = struct.pack("<h", amplitude)
    frames = frame * sample_rate * duration_seconds
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_live_smtp_notification_send():
    env = _require_env("SMTP_LIVE_HOST", "SMTP_LIVE_SENDER", "SMTP_LIVE_RECIPIENT")
    port = int(_env("SMTP_LIVE_PORT") or "587")
    use_tls = (_env("SMTP_LIVE_USE_TLS") or "true").lower() not in {"0", "false", "no"}
    service = SMTPNotificationService(
        SMTPNotificationConfig(
            host=env["SMTP_LIVE_HOST"],
            port=port,
            sender=env["SMTP_LIVE_SENDER"],
            username=_env("SMTP_LIVE_USERNAME"),
            password=_env("SMTP_LIVE_PASSWORD"),
            use_tls=use_tls,
            timeout=float(_env("SMTP_LIVE_TIMEOUT") or "30"),
        )
    )

    health = await service.health_check()
    assert health.status is HealthState.HEALTHY
    assert health.details["provider"] == "smtp"

    result = await service.send(
        "email",
        env["SMTP_LIVE_RECIPIENT"],
        "fastapi-infra live provider test",
        "This message was sent by an opt-in fastapi-infra live provider test.",
    )

    assert result.status == "sent"
    assert result.recipient == env["SMTP_LIVE_RECIPIENT"]


@pytest.mark.asyncio
async def test_live_openai_chat_and_embedding():
    _require_module("openai", "openai")
    env = _require_env(
        "OPENAI_API_KEY",
        "OPENAI_LIVE_CHAT_MODEL",
        "OPENAI_LIVE_EMBEDDING_MODEL",
    )
    provider = OpenAIProvider(
        config=AIProviderConfig(
            api_key=env["OPENAI_API_KEY"],
            base_url=_env("OPENAI_API_BASE"),
            timeout=float(_env("OPENAI_LIVE_TIMEOUT") or "30"),
        )
    )
    try:
        health = await provider.health_check()
        assert health.status is HealthState.HEALTHY
        assert health.details["provider"] == "openai"

        chat = await provider.chat(
            ChatRequest(
                model=env["OPENAI_LIVE_CHAT_MODEL"],
                messages=[
                    ChatMessage(
                        role="user",
                        content="Reply with exactly: fastapi-infra-live",
                    )
                ],
                max_tokens=16,
            )
        )
        assert chat.provider == "openai"
        _assert_exact_live_chat_response(chat.content)

        embedding = await provider.embed(
            EmbeddingRequest(
                model=env["OPENAI_LIVE_EMBEDDING_MODEL"],
                input="fastapi infra live provider test",
            )
        )
        assert embedding.provider == "openai"
        assert len(embedding.embeddings) == 1
        assert embedding.embeddings[0]
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_live_anthropic_chat():
    _require_module("anthropic", "anthropic")
    env = _require_env("ANTHROPIC_API_KEY", "ANTHROPIC_LIVE_CHAT_MODEL")
    provider = AnthropicAIProvider(
        config=AIProviderConfig(
            api_key=env["ANTHROPIC_API_KEY"],
            base_url=_env("ANTHROPIC_API_BASE"),
            timeout=float(_env("ANTHROPIC_LIVE_TIMEOUT") or "30"),
        )
    )
    try:
        health = await provider.health_check()
        assert health.status is HealthState.HEALTHY
        assert health.details["provider"] == "anthropic"

        chat = await provider.chat(
            ChatRequest(
                model=env["ANTHROPIC_LIVE_CHAT_MODEL"],
                messages=[
                    ChatMessage(
                        role="user",
                        content="Reply with exactly: fastapi-infra-live",
                    )
                ],
                max_tokens=16,
            )
        )
        assert chat.provider == "anthropic"
        _assert_exact_live_chat_response(chat.content)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_live_gemini_chat_and_embedding():
    _require_module("google.genai", "google-genai")
    env = _require_env(
        "GEMINI_API_KEY",
        "GEMINI_LIVE_CHAT_MODEL",
        "GEMINI_LIVE_EMBEDDING_MODEL",
    )
    provider = GeminiAIProvider(
        config=AIProviderConfig(
            api_key=env["GEMINI_API_KEY"],
            base_url=_env("GEMINI_API_BASE"),
            timeout=float(_env("GEMINI_LIVE_TIMEOUT") or "30"),
        )
    )
    try:
        health = await provider.health_check()
        assert health.status is HealthState.HEALTHY
        assert health.details["provider"] == "gemini"

        chat = await provider.chat(
            ChatRequest(
                model=env["GEMINI_LIVE_CHAT_MODEL"],
                messages=[
                    ChatMessage(
                        role="user",
                        content="Reply with exactly: fastapi-infra-live",
                    )
                ],
                max_tokens=16,
            )
        )
        assert chat.provider == "gemini"
        _assert_exact_live_chat_response(chat.content)

        embedding = await provider.embed(
            EmbeddingRequest(
                model=env["GEMINI_LIVE_EMBEDDING_MODEL"],
                input="fastapi infra live provider test",
            )
        )
        assert embedding.provider == "gemini"
        assert len(embedding.embeddings) == 1
        assert embedding.embeddings[0]
    finally:
        await provider.aclose()
