import asyncio

import pytest

from infra.plugins.transaction.coordinator import Operation, TransactionCoordinator


async def test_transaction_coordinator_returns_step_results() -> None:
    coordinator = TransactionCoordinator()

    result = await coordinator.execute_with_compensation(
        [
            Operation(name="reserve_inventory", execute=lambda: _return("reserved")),
            Operation(name="create_invoice", execute=lambda: _return({"invoice_id": "inv_1"})),
        ]
    )

    assert result.success is True
    assert result.completed_operations == ["reserve_inventory", "create_invoice"]
    assert result.results == {
        "reserve_inventory": "reserved",
        "create_invoice": {"invoice_id": "inv_1"},
    }
    assert result.failed_operation is None
    assert result.compensation_succeeded is True


async def test_transaction_coordinator_compensates_completed_steps_in_reverse_order() -> None:
    coordinator = TransactionCoordinator()
    events: list[str] = []

    async def execute_first() -> str:
        events.append("execute:first")
        return "ok"

    async def compensate_first() -> None:
        events.append("compensate:first")

    async def execute_second() -> None:
        events.append("execute:second")
        raise RuntimeError("gateway failed")

    result = await coordinator.execute_with_compensation(
        [
            Operation(
                name="first",
                execute=execute_first,
                compensate=compensate_first,
            ),
            Operation(name="second", execute=execute_second),
        ]
    )

    assert result.success is False
    assert result.completed_operations == ["first"]
    assert result.results == {"first": "ok"}
    assert result.failed_operation == "second"
    assert isinstance(result.error, RuntimeError)
    assert result.compensated_operations == ["first"]
    assert result.compensation_failures == []
    assert events == ["execute:first", "execute:second", "compensate:first"]


async def test_transaction_coordinator_reports_compensation_failures() -> None:
    coordinator = TransactionCoordinator()

    async def fail_compensation() -> None:
        raise RuntimeError("rollback failed")

    result = await coordinator.execute_with_compensation(
        [
            Operation(
                name="reserve",
                execute=lambda: _return("ok"),
                compensate=fail_compensation,
            ),
            Operation(name="charge", execute=lambda: _raise(RuntimeError("charge failed"))),
        ]
    )

    assert result.success is False
    assert result.failed_operation == "charge"
    assert result.compensated_operations == []
    assert len(result.compensation_failures) == 1
    assert result.compensation_failures[0].operation == "reserve"
    assert isinstance(result.compensation_failures[0].error, RuntimeError)
    assert result.compensation_succeeded is False


async def test_transaction_coordinator_can_continue_after_operation_failures() -> None:
    coordinator = TransactionCoordinator()

    result = await coordinator.execute_with_compensation(
        [
            Operation(name="first", execute=lambda: _raise(RuntimeError("first failed"))),
            Operation(name="second", execute=lambda: _return("ok")),
        ],
        continue_on_error=True,
    )

    assert result.success is False
    assert result.completed_operations == ["second"]
    assert result.results == {"second": "ok"}
    assert [failure.operation for failure in result.failed_operations] == ["first"]
    assert result.compensated_operations == []


async def test_transaction_coordinator_applies_operation_timeout() -> None:
    coordinator = TransactionCoordinator()

    async def slow_operation() -> None:
        await asyncio.sleep(0.05)

    result = await coordinator.execute_with_compensation(
        [Operation(name="slow", execute=slow_operation, timeout_seconds=0.001)]
    )

    assert result.success is False
    assert result.failed_operation == "slow"
    assert isinstance(result.error, TimeoutError)


async def test_transaction_coordinator_validates_operation_names_and_timeouts() -> None:
    coordinator = TransactionCoordinator()

    with pytest.raises(ValueError, match="duplicate operation name"):
        await coordinator.execute_with_compensation(
            [
                Operation(name="step", execute=lambda: _return(None)),
                Operation(name="step", execute=lambda: _return(None)),
            ]
        )

    with pytest.raises(ValueError, match="operation name"):
        await coordinator.execute_with_compensation(
            [Operation(name=" ", execute=lambda: _return(None))]
        )

    with pytest.raises(ValueError, match="timeout"):
        await coordinator.execute_with_compensation(
            [Operation(name="step", execute=lambda: _return(None), timeout_seconds=0)]
        )


async def _return(value: object) -> object:
    return value


async def _raise(error: Exception) -> object:
    raise error
