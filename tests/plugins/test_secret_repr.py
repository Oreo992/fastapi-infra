from infra.plugins.ai.plugin import AIProviderConfig
from infra.plugins.auth.models import HashedApiKeyRecord, JwtSigningKeyRecord
from infra.plugins.auth.plugin import AuthPluginConfig
from infra.plugins.database.plugin import DatabaseManagerConfig
from infra.plugins.http.plugin import HTTPPluginConfig
from infra.plugins.notifications.noop import SMTPNotificationConfig
from infra.plugins.payment.stripe import StripeProviderConfig
from infra.plugins.speech.providers.openai import OpenAISpeechProviderConfig
from infra.plugins.storage.s3 import S3StorageConfig


def test_provider_config_repr_does_not_expose_secrets():
    configs = [
        AIProviderConfig(api_key="sk-ai-secret"),
        StripeProviderConfig(api_key="sk-stripe-secret", webhook_secret="whsec-secret"),
        OpenAISpeechProviderConfig(api_key="sk-speech-secret"),
        S3StorageConfig(
            bucket="bucket",
            region="us-east-1",
            access_key_id="access-key-id",
            secret_access_key="s3-secret",
        ),
        SMTPNotificationConfig(
            host="smtp.example.com",
            sender="noreply@example.com",
            password="smtp-secret",
        ),
        DatabaseManagerConfig(mysql_password="mysql-secret"),
        HTTPPluginConfig(headers={"Authorization": "Bearer http-secret"}),
        HashedApiKeyRecord(subject="user-1", key_hash="hashed-api-secret"),
        JwtSigningKeyRecord(key_id="current", secret="jwt-secret"),
        AuthPluginConfig(jwt_secret="jwt-config-secret"),
    ]

    rendered = "\n".join(repr(config) for config in configs)

    for secret in (
        "sk-ai-secret",
        "sk-stripe-secret",
        "whsec-secret",
        "sk-speech-secret",
        "s3-secret",
        "smtp-secret",
        "mysql-secret",
        "http-secret",
        "hashed-api-secret",
        "jwt-secret",
        "jwt-config-secret",
    ):
        assert secret not in rendered


def test_public_identifiers_remain_visible_in_config_repr():
    rendered = repr(
        S3StorageConfig(
            bucket="bucket",
            region="us-east-1",
            access_key_id="access-key-id",
            secret_access_key="s3-secret",
        )
    )

    assert "bucket" in rendered
    assert "access-key-id" in rendered
