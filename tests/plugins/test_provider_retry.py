import pytest

from infra.plugins.retry import retry_provider_operation


class RetryableError(RuntimeError):
    pass


class FatalError(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_retry_provider_operation_retries_retryable_exceptions() -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableError("temporary")
        return "ok"

    result = await retry_provider_operation(
        operation,
        max_attempts=2,
        base_delay=0,
        is_retryable_exception=lambda exc: isinstance(exc, RetryableError),
    )

    assert result == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_retry_provider_operation_does_not_retry_fatal_exceptions() -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        raise FatalError("fatal")

    with pytest.raises(FatalError):
        await retry_provider_operation(
            operation,
            max_attempts=2,
            base_delay=0,
            is_retryable_exception=lambda exc: isinstance(exc, RetryableError),
        )

    assert attempts == 1


@pytest.mark.asyncio
async def test_retry_provider_operation_retries_retryable_results() -> None:
    statuses = [503, 200]

    async def operation() -> int:
        return statuses.pop(0)

    result = await retry_provider_operation(
        operation,
        max_attempts=2,
        base_delay=0,
        is_retryable_exception=lambda exc: False,
        is_retryable_result=lambda status: status >= 500,
    )

    assert result == 200
    assert statuses == []


@pytest.mark.asyncio
async def test_retry_provider_operation_uses_result_retry_delay(monkeypatch) -> None:
    statuses = [429, 200]
    sleeps: list[float] = []

    async def operation() -> int:
        return statuses.pop(0)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("infra.plugins.retry.asyncio.sleep", fake_sleep)

    result = await retry_provider_operation(
        operation,
        max_attempts=2,
        base_delay=10,
        is_retryable_exception=lambda exc: False,
        is_retryable_result=lambda status: status == 429,
        retry_delay_for_result=lambda status: 2 if status == 429 else None,
    )

    assert result == 200
    assert sleeps == [2]


@pytest.mark.asyncio
async def test_retry_provider_operation_returns_final_retryable_result() -> None:
    attempts = 0

    async def operation() -> int:
        nonlocal attempts
        attempts += 1
        return 503

    result = await retry_provider_operation(
        operation,
        max_attempts=2,
        base_delay=0,
        is_retryable_exception=lambda exc: False,
        is_retryable_result=lambda status: status >= 500,
    )

    assert result == 503
    assert attempts == 2


@pytest.mark.asyncio
async def test_retry_provider_operation_rejects_invalid_retry_config() -> None:
    async def operation() -> str:
        return "ok"

    with pytest.raises(ValueError, match="max_attempts"):
        await retry_provider_operation(
            operation,
            max_attempts=0,
            base_delay=0,
            is_retryable_exception=lambda exc: False,
        )

    with pytest.raises(ValueError, match="base_delay"):
        await retry_provider_operation(
            operation,
            max_attempts=1,
            base_delay=-0.1,
            is_retryable_exception=lambda exc: False,
        )
