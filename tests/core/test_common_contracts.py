from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

import infra.common as common
import infra.common.contracts as contracts
from infra.common import ApiResponse, ErrorCode, PaginatedResponse, PaginationParams


def test_common_public_api_exposes_single_response_contract() -> None:
    assert sorted(common.__all__) == [
        "ApiResponse",
        "ErrorCode",
        "ErrorDetail",
        "PaginatedResponse",
        "PaginationParams",
    ]
    assert not hasattr(contracts, "StandardResponse")
    assert not hasattr(contracts, "HealthResponse")


def test_api_response_success_factory_uses_single_envelope() -> None:
    response = ApiResponse.ok({"id": "order-1"}, trace_id="trace-123")

    assert response.success is True
    assert response.data == {"id": "order-1"}
    assert response.error is None
    assert response.trace_id == "trace-123"
    assert response.timestamp.endswith("+00:00")


def test_api_response_failure_factory_sets_error_trace() -> None:
    response: ApiResponse[Any] = ApiResponse.fail(
        ErrorCode.NOT_FOUND,
        "missing",
        details={"resource": "invoice"},
        trace_id="trace-404",
    )

    assert response.success is False
    assert response.data is None
    assert response.error is not None
    assert response.error.code is ErrorCode.NOT_FOUND
    assert response.error.message == "missing"
    assert response.error.details == {"resource": "invoice"}
    assert response.error.trace_id == "trace-404"
    assert response.trace_id == "trace-404"


def test_api_response_accepts_datetime_timestamp() -> None:
    response = ApiResponse[str].model_validate(
        {
            "success": True,
            "data": "ok",
            "timestamp": datetime(2026, 5, 13, tzinfo=UTC),
        }
    )

    assert response.timestamp == "2026-05-13T00:00:00+00:00"


def test_api_response_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ApiResponse.model_validate({"success": True, "ignored": True})


def test_pagination_params_exposes_offset_and_bounds() -> None:
    params = PaginationParams(page=3, size=25)

    assert params.offset == 50

    with pytest.raises(ValidationError):
        PaginationParams(page=0)
    with pytest.raises(ValidationError):
        PaginationParams(size=101)


def test_paginated_response_create_computes_pages_and_validates_bounds() -> None:
    page = PaginatedResponse.create(items=[{"id": 1}], total=41, page=2, size=20)

    assert page.items == [{"id": 1}]
    assert page.total == 41
    assert page.page == 2
    assert page.size == 20
    assert page.pages == 3

    with pytest.raises(ValidationError):
        PaginatedResponse.create(items=[], total=-1, page=1, size=20)
