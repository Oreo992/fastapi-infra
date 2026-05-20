import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def retry_provider_operation(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_delay: float,
    is_retryable_exception: Callable[[Exception], bool],
    is_retryable_result: Callable[[T], bool] | None = None,
    retry_delay_for_exception: Callable[[Exception], float | None] | None = None,
    retry_delay_for_result: Callable[[T], float | None] | None = None,
    exhausted_message: str = "provider max_attempts must allow at least one request",
) -> T:
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    if base_delay < 0:
        raise ValueError("base_delay must be non-negative")

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            result = await operation()
        except Exception as exc:
            last_error = exc
            if not is_retryable_exception(exc) or attempt == max_attempts - 1:
                raise
            await asyncio.sleep(
                _retry_delay(
                    attempt,
                    base_delay,
                    retry_delay_for_exception(exc) if retry_delay_for_exception else None,
                )
            )
            continue

        if is_retryable_result is None or not is_retryable_result(result):
            return result
        if attempt == max_attempts - 1:
            return result
        await asyncio.sleep(
            _retry_delay(
                attempt,
                base_delay,
                retry_delay_for_result(result) if retry_delay_for_result else None,
            )
        )

    if last_error is not None:
        raise last_error
    raise RuntimeError(exhausted_message)


def _retry_delay(attempt: int, base_delay: float, provider_delay: float | None) -> float:
    if provider_delay is not None and provider_delay >= 0:
        return float(provider_delay)
    return float(base_delay * (2**attempt))
