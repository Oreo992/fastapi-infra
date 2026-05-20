import asyncio
import hashlib
import hmac
import json
import smtplib
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from typing import Any, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from infra.config.models import InfraSettings
from infra.core.health import HealthState, HealthStatus
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.plugins.notifications.registry import NotificationProviderRegistry
from infra.plugins.provider_extensions import (
    external_provider_names_to_load,
    load_entry_point_provider,
)
from infra.plugins.provider_health import provider_health_status
from infra.plugins.release_checks import (
    PluginProviderCertification,
    PluginProviderPolicy,
    PluginReleaseIssue,
    provider_certification,
    provider_policy,
    release_error,
)
from infra.plugins.retry import retry_provider_operation

T = TypeVar("T")

NOTIFICATION_PROVIDER_ENTRY_POINT_GROUP = "fastapi_infra.notification_providers"


class NotificationResult(BaseModel):
    id: str
    channel: str
    recipient: str
    subject: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str


class NoopNotificationService:
    name = "noop"

    def __init__(self) -> None:
        self.results: list[NotificationResult] = []

    async def send(
        self,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        result = NotificationResult(
            id=f"ntf_{uuid4().hex}",
            channel=channel,
            recipient=recipient,
            subject=subject,
            body=body,
            metadata=metadata or {},
            status="skipped",
        )
        self.results.append(result)
        return result


class SMTPNotificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(default=587, gt=0, le=65535)
    sender: str = Field(min_length=1)
    username: str | None = None
    password: str | None = Field(default=None, repr=False)
    use_tls: bool = True
    timeout: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    retry_base_delay: float = Field(default=0.25, ge=0)

    @field_validator("host", "sender", mode="before")
    @classmethod
    def _strip_required_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @field_validator("port", "timeout", "max_attempts", "retry_base_delay", mode="before")
    @classmethod
    def _reject_bool_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value


class SMTPNotificationError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class SMTPNotificationService:
    name = "smtp"

    def __init__(
        self,
        config: SMTPNotificationConfig,
        smtp_factory: Any | None = None,
    ) -> None:
        self.config = config
        self._smtp_factory = smtp_factory or smtplib.SMTP

    async def send(
        self,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        if channel != "email":
            raise ValueError("smtp notifications only support email channel")

        message = EmailMessage()
        message["From"] = self.config.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)

        await self._with_retry(lambda: asyncio.to_thread(self._send_message, message))

        return NotificationResult(
            id=f"ntf_{uuid4().hex}",
            channel=channel,
            recipient=recipient,
            subject=subject,
            body=body,
            metadata=metadata or {},
            status="sent",
        )

    def _send_message(self, message: EmailMessage) -> None:
        client = None
        try:
            client = self._smtp_factory(
                self.config.host,
                self.config.port,
                timeout=self.config.timeout,
            )
            if self.config.use_tls:
                client.starttls()
            if self.config.username is not None:
                client.login(self.config.username, self.config.password or "")
            client.send_message(message)
        except Exception as exc:
            raise _smtp_error(exc) from exc
        finally:
            if client is not None:
                client.quit()

    async def health_check(self) -> HealthStatus:
        try:
            await self._with_retry(lambda: asyncio.to_thread(self._probe_connection))
        except Exception as exc:
            return HealthStatus(
                name=self.name,
                status=HealthState.UNHEALTHY,
                message=str(exc) or exc.__class__.__name__,
                details={
                    "provider": self.name,
                    "host": self.config.host,
                    "port": self.config.port,
                },
            )
        return HealthStatus(
            name=self.name,
            status=HealthState.HEALTHY,
            details={
                "provider": self.name,
                "host": self.config.host,
                "port": self.config.port,
            },
        )

    def _probe_connection(self) -> None:
        client = None
        try:
            client = self._smtp_factory(
                self.config.host,
                self.config.port,
                timeout=self.config.timeout,
            )
            if self.config.use_tls:
                client.starttls()
            if self.config.username is not None:
                client.login(self.config.username, self.config.password or "")
        except Exception as exc:
            raise _smtp_error(exc) from exc
        finally:
            if client is not None:
                client.quit()

    async def _with_retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        return await retry_provider_operation(
            operation,
            max_attempts=self.config.max_attempts,
            base_delay=self.config.retry_base_delay,
            is_retryable_exception=lambda exc: isinstance(exc, SMTPNotificationError)
            and exc.retryable,
            exhausted_message="smtp max_attempts must allow at least one request",
        )


def _smtp_error(exc: Exception) -> SMTPNotificationError:
    if isinstance(exc, SMTPNotificationError):
        return exc
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return SMTPNotificationError(str(exc), retryable=False)
    if isinstance(exc, smtplib.SMTPRecipientsRefused | smtplib.SMTPSenderRefused):
        return SMTPNotificationError(str(exc), retryable=False)
    if isinstance(exc, smtplib.SMTPResponseException):
        retryable = exc.smtp_code in {421, 450, 451, 452}
        return SMTPNotificationError(str(exc), retryable=retryable)
    if isinstance(exc, smtplib.SMTPServerDisconnected | OSError | TimeoutError):
        return SMTPNotificationError(str(exc), retryable=True)
    if isinstance(exc, smtplib.SMTPException):
        return SMTPNotificationError(str(exc), retryable=False)
    return SMTPNotificationError(str(exc) or exc.__class__.__name__, retryable=True)


class WebhookNotificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    health_url: str | None = None
    signing_secret: str | None = Field(default=None, repr=False)
    signature_header: str = "x-infra-signature"
    timestamp_header: str = "x-infra-timestamp"
    timeout: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    retry_base_delay: float = Field(default=0.25, ge=0)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("url", "health_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an absolute http(s) URL")
        return value

    @field_validator("signature_header", "timestamp_header")
    @classmethod
    def _validate_header_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("header name must not be blank")
        return normalized

    @field_validator("timeout", "max_attempts", "retry_base_delay", mode="before")
    @classmethod
    def _reject_bool_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value


class WebhookNotificationError(RuntimeError):
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


class WebhookNotificationService:
    name = "webhook"

    def __init__(
        self,
        config: WebhookNotificationConfig,
        opener: Any | None = None,
    ) -> None:
        self.config = config
        self._opener = opener or urllib.request.urlopen

    async def send(
        self,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> NotificationResult:
        result = NotificationResult(
            id=f"ntf_{uuid4().hex}",
            channel=channel,
            recipient=recipient,
            subject=subject,
            body=body,
            metadata=metadata or {},
            status="sent",
        )
        payload = result.model_dump()
        await self._with_retry(
            lambda: asyncio.to_thread(
                self._post_json,
                self.config.url,
                payload,
                "POST",
            )
        )
        return result

    async def health_check(self) -> HealthStatus:
        health_url = self.config.health_url
        if health_url is None:
            return HealthStatus(
                name=self.name,
                status=HealthState.DEGRADED,
                message="webhook health_url is not configured",
                details={"provider": self.name},
            )
        try:
            await self._with_retry(
                lambda: asyncio.to_thread(
                    self._post_json,
                    health_url,
                    {"probe": "notifications"},
                    "GET",
                )
            )
        except Exception as exc:
            return HealthStatus(
                name=self.name,
                status=HealthState.UNHEALTHY,
                message=str(exc) or exc.__class__.__name__,
                details={"provider": self.name},
            )
        return HealthStatus(
            name=self.name,
            status=HealthState.HEALTHY,
            details={"provider": self.name},
        )

    def _post_json(self, url: str, payload: dict[str, Any], method: str) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {
            "content-type": "application/json",
            **self.config.headers,
        }
        if self.config.signing_secret is not None:
            timestamp = str(int(time.time()))
            signed_payload = timestamp.encode("ascii") + b"." + body
            signature = hmac.new(
                self.config.signing_secret.encode("utf-8"),
                signed_payload,
                hashlib.sha256,
            ).hexdigest()
            headers[self.config.timestamp_header] = timestamp
            headers[self.config.signature_header] = f"v1={signature}"

        request = urllib.request.Request(
            url,
            data=body if method != "GET" else None,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self.config.timeout) as response:
                status_code = int(response.status)
                if status_code < 200 or status_code >= 300:
                    raise WebhookNotificationError(
                        f"webhook returned HTTP {status_code}",
                        status_code=status_code,
                        retryable=_retryable_webhook_status(status_code),
                    )
        except urllib.error.HTTPError as exc:
            raise WebhookNotificationError(
                f"webhook returned HTTP {exc.code}",
                status_code=exc.code,
                retryable=_retryable_webhook_status(exc.code),
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise WebhookNotificationError(
                str(exc) or exc.__class__.__name__,
                retryable=True,
            ) from exc

    async def _with_retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        return await retry_provider_operation(
            operation,
            max_attempts=self.config.max_attempts,
            base_delay=self.config.retry_base_delay,
            is_retryable_exception=lambda exc: isinstance(exc, WebhookNotificationError)
            and exc.retryable,
            exhausted_message="webhook max_attempts must allow at least one request",
        )


def _retryable_webhook_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429, 500, 502, 503, 504}


class NotificationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider: str = "noop"
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    health_probe: bool = False


class NotificationsPlugin:
    metadata = PluginMetadata(
        name="notifications",
        version="1.0.0",
        default_enabled=False,
        provides=["notifications"],
    )
    config_model = NotificationsConfig
    manifest_hints = {
        "service_keys": {"notifications": "infra.plugins.NOTIFICATIONS_SERVICE"},
        "env_vars": [
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_SENDER",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "WEBHOOK_NOTIFICATION_URL",
            "WEBHOOK_NOTIFICATION_HEALTH_URL",
            "WEBHOOK_NOTIFICATION_SIGNING_SECRET",
        ],
        "local_config_example": {
            "default_provider": "noop",
        },
        "production_config_example": {
            "default_provider": "smtp",
            "health_probe": True,
            "providers": {
                "smtp": {
                    "host": "${SMTP_HOST}",
                    "port": "${SMTP_PORT}",
                    "sender": "${SMTP_SENDER}",
                    "username": "${SMTP_USERNAME}",
                    "password": "${SMTP_PASSWORD}",
                }
            },
        },
        "release_check_notes": [
            "Production cannot use the noop provider.",
            "SMTP requires health_probe=true and provider certification.",
            "Webhook notifications require a signing_secret and health_url in production.",
        ],
    }

    def validate_config(self, config: NotificationsConfig | None) -> None:
        config = config if isinstance(config, NotificationsConfig) else NotificationsConfig()
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "noop" in provider_names:
            registered_names.add("noop")
        if "smtp" in provider_names:
            SMTPNotificationConfig.model_validate(config.providers.get("smtp", {}))
            registered_names.add("smtp")
        if "webhook" in provider_names:
            WebhookNotificationConfig.model_validate(config.providers.get("webhook", {}))
            registered_names.add("webhook")
        external_provider_names_to_load(
            provider_kind="notifications",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=NOTIFICATION_PROVIDER_ENTRY_POINT_GROUP,
        )

    def release_check(
        self,
        settings: InfraSettings,
        config: NotificationsConfig,
    ) -> list[PluginReleaseIssue]:
        issues: list[PluginReleaseIssue] = []
        provider_names = set(config.providers) | {config.default_provider}
        if config.default_provider == "noop":
            issues.append(
                release_error(
                    "noop_provider",
                    "production notifications cannot use noop provider",
                )
            )
        if "smtp" in provider_names:
            try:
                SMTPNotificationConfig.model_validate(config.providers.get("smtp", {}))
            except (ValidationError, ValueError) as exc:
                issues.append(release_error("smtp_config_invalid", str(exc)))
        if config.default_provider == "webhook":
            try:
                webhook_config = WebhookNotificationConfig.model_validate(
                    config.providers.get("webhook", {})
                )
            except (ValidationError, ValueError) as exc:
                issues.append(release_error("webhook_config_invalid", str(exc)))
                webhook_config = None
            if webhook_config is not None and webhook_config.signing_secret is None:
                issues.append(
                    release_error(
                        "webhook_signing_secret_required",
                        "webhook notifications require signing_secret in production",
                    )
                )
            if webhook_config is not None and webhook_config.health_url is None:
                issues.append(
                    release_error(
                        "webhook_health_url_required",
                        "webhook notifications require health_url in production",
                    )
                )
            if not config.health_probe:
                issues.append(
                    release_error(
                        "health_probe_required",
                        "external provider must enable health_probe in production",
                    )
                )
        return issues

    def provider_certifications(
        self,
        settings: InfraSettings,
        config: NotificationsConfig,
    ) -> list[PluginProviderCertification]:
        return [provider_certification("notifications", config.default_provider)]

    def provider_release_policies(
        self,
        settings: InfraSettings,
        config: NotificationsConfig,
    ) -> list[PluginProviderPolicy]:
        return [
            provider_policy(
                "notifications",
                {config.default_provider},
                local_providers={"noop", "webhook"},
                health_probe=config.health_probe,
            )
        ]

    def register(self, ctx: PluginContext) -> None:
        config = (
            ctx.config if isinstance(ctx.config, NotificationsConfig) else NotificationsConfig()
        )
        registry = NotificationProviderRegistry(default_provider=config.default_provider)
        provider_names = set(config.providers) | {config.default_provider}
        registered_names: set[str] = set()
        if "noop" in provider_names:
            registry.register(
                NoopNotificationService(),
                default=config.default_provider == "noop",
            )
            registered_names.add("noop")
        if "smtp" in provider_names:
            smtp_config = SMTPNotificationConfig.model_validate(config.providers.get("smtp", {}))
            registry.register(
                SMTPNotificationService(smtp_config),
                default=config.default_provider == "smtp",
            )
            registered_names.add("smtp")
        if "webhook" in provider_names:
            webhook_config = WebhookNotificationConfig.model_validate(
                config.providers.get("webhook", {})
            )
            registry.register(
                WebhookNotificationService(webhook_config),
                default=config.default_provider == "webhook",
            )
            registered_names.add("webhook")
        for provider_name in external_provider_names_to_load(
            provider_kind="notifications",
            requested_names=provider_names,
            registered_names=registered_names,
            entry_point_group=NOTIFICATION_PROVIDER_ENTRY_POINT_GROUP,
        ):
            registry.register(
                load_entry_point_provider(
                    NOTIFICATION_PROVIDER_ENTRY_POINT_GROUP,
                    provider_name,
                    config.providers.get(provider_name, {}),
                    required_methods=("send",),
                ),
                default=config.default_provider == provider_name,
            )
        registry.get()
        ctx.services["notifications"] = registry

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext) -> HealthStatus:
        config = (
            ctx.config if isinstance(ctx.config, NotificationsConfig) else NotificationsConfig()
        )
        notifications = ctx.services.get("notifications")
        if not isinstance(notifications, NotificationProviderRegistry):
            return ctx.health_status(
                "notifications",
                HealthState.UNHEALTHY,
                "notifications registry missing",
            )
        if config.default_provider != "noop":
            if config.health_probe:
                return await provider_health_status(
                    ctx,
                    "notifications",
                    notifications.get(config.default_provider),
                    local_provider_names={"noop"},
                )
            return ctx.health_status(
                "notifications",
                HealthState.DEGRADED,
                "external provider configured; upstream is not checked by health",
                {"provider": config.default_provider},
            )
        return ctx.health_status(
            "notifications",
            HealthState.DEGRADED,
            "noop notifications provider is enabled; messages are not delivered",
            {"provider": "noop"},
        )
