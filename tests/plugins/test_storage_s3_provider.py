from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from infra.core.health import HealthState
from infra.plugins.storage import S3Storage, S3StorageConfig, S3StorageError
from infra.plugins.storage.s3 import S3HTTPResponse


class FakeResponse(S3HTTPResponse):
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        super().__init__(status_code=status_code, content=content)


class FakeTransport:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes,
    ) -> S3HTTPResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "content": content,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200)


def make_storage(*responses: FakeResponse, force_path_style: bool = True) -> S3Storage:
    return S3Storage(
        S3StorageConfig(
            bucket="assets",
            region="us-east-1",
            access_key_id="AKIDEXAMPLE",
            secret_access_key="secret",
            endpoint_url="https://s3.example.test",
            force_path_style=force_path_style,
        ),
        transport=FakeTransport(*responses),
    )


def test_s3_storage_config_requires_credentials_and_bucket():
    with pytest.raises(ValidationError):
        S3StorageConfig(
            bucket="",
            region="us-east-1",
            access_key_id="",
            secret_access_key="",
        )


def test_s3_storage_config_accepts_retry_options():
    config = S3StorageConfig(
        bucket="assets",
        region="us-east-1",
        access_key_id="key",
        secret_access_key="secret",
        max_attempts=2,
        retry_base_delay=0,
    )

    assert config.max_attempts == 2
    assert config.retry_base_delay == 0


def test_urllib_s3_transport_uses_configured_timeout(monkeypatch):
    import infra.plugins.storage.s3 as s3_module

    calls = []

    class FakeHTTPResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout):
        calls.append({"request": request, "timeout": timeout})
        return FakeHTTPResponse()

    monkeypatch.setattr(s3_module, "urlopen", fake_urlopen)
    transport = s3_module.UrllibS3Transport(timeout=7.5)

    response = transport._request("GET", "https://s3.example.test/assets/file.txt", {}, b"")

    assert response.status_code == 200
    assert response.content == b"ok"
    assert calls[0]["timeout"] == 7.5


@pytest.mark.asyncio
async def test_s3_health_check_probes_bucket_head():
    storage = make_storage(FakeResponse(200))

    status = await storage.health_check()

    request = storage.transport.requests[0]
    assert status.status is HealthState.HEALTHY
    assert status.details == {"provider": "s3", "bucket": "assets"}
    assert request["method"] == "HEAD"
    assert request["url"] == "https://s3.example.test/assets"
    assert request["content"] == b""


@pytest.mark.asyncio
async def test_s3_health_check_reports_upstream_failure():
    storage = make_storage(FakeResponse(403, b"access denied"))

    status = await storage.health_check()

    assert status.status is HealthState.UNHEALTHY
    assert status.message == "S3 bucket probe failed with status 403"
    assert status.details == {"provider": "s3", "bucket": "assets"}


@pytest.mark.asyncio
async def test_put_object_sends_signed_put_to_path_style_bucket_path():
    storage = make_storage(FakeResponse(200))

    await storage.put_object(
        "nested/file.txt",
        b"payload",
        content_type="text/plain",
        metadata={"trace-id": "abc"},
    )

    request = storage.transport.requests[0]
    assert request["method"] == "PUT"
    assert request["url"] == "https://s3.example.test/assets/nested/file.txt"
    assert request["content"] == b"payload"
    assert request["headers"]["Content-Type"] == "text/plain"
    assert request["headers"]["x-amz-meta-trace-id"] == "abc"
    assert request["headers"]["x-amz-content-sha256"]
    assert request["headers"]["x-amz-date"]
    assert request["headers"]["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"
    )
    assert "SignedHeaders=" in request["headers"]["Authorization"]
    assert "Signature=" in request["headers"]["Authorization"]


@pytest.mark.asyncio
async def test_get_object_sends_get_and_returns_response_bytes():
    storage = make_storage(FakeResponse(200, b"payload"))

    data = await storage.get_object("nested/file.txt")

    request = storage.transport.requests[0]
    assert data == b"payload"
    assert request["method"] == "GET"
    assert request["url"] == "https://s3.example.test/assets/nested/file.txt"


@pytest.mark.asyncio
async def test_s3_retries_retryable_status_before_succeeding():
    storage = S3Storage(
        S3StorageConfig(
            bucket="assets",
            region="us-east-1",
            access_key_id="AKIDEXAMPLE",
            secret_access_key="secret",
            endpoint_url="https://s3.example.test",
            force_path_style=True,
            max_attempts=2,
            retry_base_delay=0,
        ),
        transport=FakeTransport(FakeResponse(503, b"slow down"), FakeResponse(200, b"payload")),
    )

    data = await storage.get_object("nested/file.txt")

    assert data == b"payload"
    assert len(storage.transport.requests) == 2


@pytest.mark.asyncio
async def test_s3_does_not_retry_non_retryable_status():
    storage = S3Storage(
        S3StorageConfig(
            bucket="assets",
            region="us-east-1",
            access_key_id="AKIDEXAMPLE",
            secret_access_key="secret",
            endpoint_url="https://s3.example.test",
            force_path_style=True,
            max_attempts=2,
            retry_base_delay=0,
        ),
        transport=FakeTransport(FakeResponse(403, b"access denied"), FakeResponse(200)),
    )

    with pytest.raises(S3StorageError) as exc:
        await storage.get_object("blocked.txt")

    assert exc.value.status_code == 403
    assert exc.value.retryable is False
    assert len(storage.transport.requests) == 1


@pytest.mark.asyncio
async def test_delete_object_sends_delete_request():
    storage = make_storage(FakeResponse(204))

    await storage.delete_object("nested/file.txt")

    request = storage.transport.requests[0]
    assert request["method"] == "DELETE"
    assert request["url"] == "https://s3.example.test/assets/nested/file.txt"


@pytest.mark.asyncio
async def test_exists_sends_head_and_returns_false_on_404():
    storage = make_storage(FakeResponse(404, b"<Error><Code>NoSuchKey</Code></Error>"))

    assert await storage.exists("missing.txt") is False

    request = storage.transport.requests[0]
    assert request["method"] == "HEAD"
    assert request["url"] == "https://s3.example.test/assets/missing.txt"


@pytest.mark.asyncio
async def test_s3_error_response_raises_clear_exception():
    storage = make_storage(FakeResponse(403, b"access denied"))

    with pytest.raises(S3StorageError, match="S3 PUT failed with status 403"):
        await storage.put_object("blocked.txt", b"payload")


@pytest.mark.asyncio
async def test_list_objects_sends_list_objects_v2_request_and_parses_xml_keys():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Name>assets</Name>
      <Prefix>nested/</Prefix>
      <Contents><Key>nested/a.txt</Key></Contents>
      <Contents><Key>nested/b space.txt</Key></Contents>
    </ListBucketResult>
    """
    storage = make_storage(FakeResponse(200, xml))

    keys = await storage.list_objects("nested/")

    request = storage.transport.requests[0]
    parsed = urlsplit(request["url"])
    query = parse_qs(parsed.query)
    assert keys == ["nested/a.txt", "nested/b space.txt"]
    assert request["method"] == "GET"
    assert parsed.scheme == "https"
    assert parsed.netloc == "s3.example.test"
    assert parsed.path == "/assets"
    assert query == {"list-type": ["2"], "prefix": ["nested/"]}
    assert request["headers"]["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"
    )


@pytest.mark.asyncio
async def test_list_objects_uses_virtual_hosted_style_endpoint_when_configured():
    storage = make_storage(FakeResponse(200, b"<ListBucketResult />"), force_path_style=False)

    assert await storage.list_objects() == []

    request = storage.transport.requests[0]
    parsed = urlsplit(request["url"])
    assert request["method"] == "GET"
    assert parsed.netloc == "assets.s3.example.test"
    assert parsed.path == ""
    assert parse_qs(parsed.query) == {"list-type": ["2"]}


@pytest.mark.asyncio
async def test_list_objects_wraps_invalid_xml_response():
    storage = make_storage(FakeResponse(200, b"<ListBucketResult>"))

    with pytest.raises(S3StorageError, match="invalid XML") as exc:
        await storage.list_objects()

    assert exc.value.response_body == b"<ListBucketResult>"
    assert exc.value.retryable is False


class FrozenDateTime:
    @classmethod
    def now(cls, tz):
        from datetime import datetime

        return datetime(2013, 5, 24, 0, 0, 0, tzinfo=tz)


def test_presign_get_url_builds_real_sigv4_query_url_for_path_style(monkeypatch):
    import infra.plugins.storage.s3 as s3_module

    monkeypatch.setattr(s3_module, "datetime", FrozenDateTime)
    storage = make_storage(force_path_style=True)

    url = storage.presign_get_url("nested/file.txt", expires_seconds=900)

    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "s3.example.test"
    assert parsed.path == "/assets/nested/file.txt"
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Credential"] == ["AKIDEXAMPLE/20130524/us-east-1/s3/aws4_request"]
    assert query["X-Amz-Date"] == ["20130524T000000Z"]
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert query["X-Amz-Signature"] == [
        "f69a30f56e0a3c100d640a36ffd902c24ffbcaf156cf9a4f85a37825c2fefc4b"
    ]


def test_presign_get_url_supports_virtual_hosted_style_endpoint(monkeypatch):
    import infra.plugins.storage.s3 as s3_module

    monkeypatch.setattr(s3_module, "datetime", FrozenDateTime)
    storage = make_storage(force_path_style=False)

    url = storage.presign_get_url("nested/file.txt")

    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "assets.s3.example.test"
    assert parsed.path == "/nested/file.txt"
    assert query["X-Amz-Expires"] == ["3600"]
