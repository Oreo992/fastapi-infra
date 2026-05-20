from typing import Any

import pytest

from infra.core.services import ServiceKey
from infra.plugins import PAYMENT_SERVICE
from infra.plugins.ratelimit import MemoryRateLimiter
from infra.plugins.services import (
    AI_SERVICE,
    AUTH_SERVICE,
    CACHE_SERVICE,
    DATABASE_SERVICE,
    HTTP_SERVICE,
    NOTIFICATIONS_SERVICE,
    OBSERVABILITY_SERVICE,
    RATELIMIT_SERVICE,
    SPEECH_SERVICE,
    STORAGE_SERVICE,
    TASKS_SERVICE,
    WEBHOOKS_SERVICE,
)
from infra.plugins.storage import LocalStorage
from infra.plugins.tasks import MemoryTaskQueue


def test_builtin_service_keys_expose_default_service_names() -> None:
    service_keys: list[ServiceKey[Any]] = [
        AI_SERVICE,
        AUTH_SERVICE,
        CACHE_SERVICE,
        DATABASE_SERVICE,
        HTTP_SERVICE,
        NOTIFICATIONS_SERVICE,
        OBSERVABILITY_SERVICE,
        PAYMENT_SERVICE,
        RATELIMIT_SERVICE,
        SPEECH_SERVICE,
        STORAGE_SERVICE,
        TASKS_SERVICE,
        WEBHOOKS_SERVICE,
    ]

    assert {key.name for key in service_keys} == {
        "ai",
        "auth",
        "cache",
        "database",
        "http",
        "notifications",
        "observability",
        "payment",
        "ratelimit",
        "speech",
        "storage",
        "tasks",
        "webhooks",
    }


def test_protocol_service_keys_validate_structural_services(tmp_path) -> None:
    assert TASKS_SERVICE.validate(MemoryTaskQueue()) is not None
    assert STORAGE_SERVICE.validate(LocalStorage(tmp_path)) is not None
    assert RATELIMIT_SERVICE.validate(MemoryRateLimiter()) is not None


def test_service_key_runtime_validation_rejects_wrong_service_type() -> None:
    with pytest.raises(RuntimeError, match="infra service has unexpected type: payment"):
        PAYMENT_SERVICE.validate(object())


def test_plugins_public_api_exports_builtin_service_keys() -> None:
    assert isinstance(PAYMENT_SERVICE, ServiceKey)
    assert PAYMENT_SERVICE.name == "payment"
