import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, field_validator

from infra.core.health import HealthState, HealthStatus
from infra.plugins.retry import retry_provider_operation


class S3StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    bucket: str = Field(min_length=1)
    region: str = Field(default="us-east-1", min_length=1)
    access_key_id: str = Field(min_length=1)
    secret_access_key: str = Field(min_length=1, repr=False)
    endpoint_url: str | None = None
    force_path_style: bool = False
    timeout: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    retry_base_delay: float = Field(default=0.25, ge=0)

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("endpoint_url must be an absolute http(s) URL")
        return value.rstrip("/")

    @field_validator("timeout", "max_attempts", "retry_base_delay", mode="before")
    @classmethod
    def reject_bool_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value


@dataclass
class S3HTTPResponse:
    status_code: int
    content: bytes


class S3Transport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes,
    ) -> S3HTTPResponse: ...


class UrllibS3Transport:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes,
    ) -> S3HTTPResponse:
        return await asyncio.to_thread(self._request, method, url, headers, content)

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes,
    ) -> S3HTTPResponse:
        request = Request(url=url, data=content, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return S3HTTPResponse(response.status, response.read())
        except HTTPError as exc:
            return S3HTTPResponse(exc.code, exc.read())
        except URLError as exc:
            raise S3StorageError(f"S3 {method} transport error: {exc}", retryable=True) from exc


class S3StorageError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: bytes | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.retryable = retryable


class S3Storage:
    name = "s3"
    retry_status_codes = frozenset({409, 429, 500, 502, 503, 504})

    def __init__(
        self,
        config: S3StorageConfig,
        *,
        transport: S3Transport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibS3Transport(timeout=config.timeout)

    async def health_check(self) -> HealthStatus:
        details = {"provider": "s3", "bucket": self.config.bucket}
        try:
            response = await self._send_url("HEAD", self._bucket_url({}), b"", {})
        except Exception as exc:
            return HealthStatus(
                name="s3",
                status=HealthState.UNHEALTHY,
                message=str(exc) or exc.__class__.__name__,
                details=details,
            )
        if response.status_code == 200:
            return HealthStatus(name="s3", status=HealthState.HEALTHY, details=details)
        return HealthStatus(
            name="s3",
            status=HealthState.UNHEALTHY,
            message=f"S3 bucket probe failed with status {response.status_code}",
            details=details,
        )

    async def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        for name, value in (metadata or {}).items():
            headers[f"x-amz-meta-{name.lower()}"] = value
        response = await self._send("PUT", key, data, headers)
        self._raise_for_status("PUT", response, allowed={200, 201, 204})

    async def get_object(self, key: str) -> bytes:
        response = await self._send("GET", key, b"", {})
        self._raise_for_status("GET", response, allowed={200, 206})
        return response.content

    async def delete_object(self, key: str) -> None:
        response = await self._send("DELETE", key, b"", {})
        self._raise_for_status("DELETE", response, allowed={200, 202, 204})

    async def exists(self, key: str) -> bool:
        response = await self._send("HEAD", key, b"", {})
        if response.status_code == 404:
            return False
        self._raise_for_status("HEAD", response, allowed={200})
        return True

    async def list_objects(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        continuation_token: str | None = None
        while True:
            query: dict[str, str] = {"list-type": "2"}
            if prefix:
                query["prefix"] = prefix
            if continuation_token:
                query["continuation-token"] = continuation_token
            url = self._bucket_url(query)
            response = await self._send_url("GET", url, b"", {})
            self._raise_for_status("GET", response, allowed={200})
            page_keys, is_truncated, continuation_token = self._parse_list_objects_v2(
                response.content
            )
            keys.extend(page_keys)
            if not is_truncated or not continuation_token:
                return keys

    def presign_get_url(self, key: str, expires_seconds: int = 3600) -> str:
        if expires_seconds <= 0:
            raise ValueError("expires_seconds must be positive")
        if expires_seconds > 604800:
            raise ValueError("expires_seconds must be at most 604800")

        url = self._object_url(key)
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_scope = now.strftime("%Y%m%d")
        credential_scope = f"{date_scope}/{self.config.region}/s3/aws4_request"
        parsed = urlsplit(url)
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.config.access_key_id}/{credential_scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires_seconds),
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = self._canonical_query_string(query)
        canonical_request = "\n".join(
            [
                "GET",
                parsed.path or "/",
                canonical_query,
                f"host:{parsed.netloc}\n",
                "host",
                "UNSIGNED-PAYLOAD",
            ]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(date_scope),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signed_query = f"{canonical_query}&X-Amz-Signature={signature}"
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, signed_query, parsed.fragment)
        )

    async def _send(
        self,
        method: str,
        key: str,
        content: bytes,
        headers: dict[str, str],
    ) -> S3HTTPResponse:
        url = self._object_url(key)
        return await self._send_url(method, url, content, headers)

    async def _send_url(
        self,
        method: str,
        url: str,
        content: bytes,
        headers: dict[str, str],
    ) -> S3HTTPResponse:
        async def send_once() -> S3HTTPResponse:
            signed_headers = self._sign(method, url, content, headers)
            try:
                return await self.transport.request(method, url, signed_headers, content)
            except S3StorageError:
                raise
            except Exception as exc:
                raise S3StorageError(
                    f"S3 {method} transport error: {exc}",
                    retryable=True,
                ) from exc

        return await retry_provider_operation(
            send_once,
            max_attempts=self.config.max_attempts,
            base_delay=self.config.retry_base_delay,
            is_retryable_exception=lambda exc: isinstance(exc, S3StorageError) and exc.retryable,
            is_retryable_result=lambda response: response.status_code in self.retry_status_codes,
            exhausted_message="S3 max_attempts must allow at least one request",
        )

    def _object_url(self, key: str) -> str:
        if not key:
            raise ValueError("S3 object key must not be empty")

        escaped_key = quote(key.lstrip("/"), safe="/~")
        if self.config.endpoint_url:
            parsed = urlsplit(self.config.endpoint_url)
            base_path = parsed.path.rstrip("/")
            if self.config.force_path_style:
                path = f"{base_path}/{quote(self.config.bucket, safe='')}/{escaped_key}"
                netloc = parsed.netloc
            else:
                path = f"{base_path}/{escaped_key}"
                netloc = f"{self.config.bucket}.{parsed.netloc}"
            return urlunsplit((parsed.scheme, netloc, path, "", ""))

        netloc = f"{self.config.bucket}.s3.{self.config.region}.amazonaws.com"
        return urlunsplit(("https", netloc, f"/{escaped_key}", "", ""))

    def _bucket_url(self, query: dict[str, str]) -> str:
        query_string = self._canonical_query_string(query)
        if self.config.endpoint_url:
            parsed = urlsplit(self.config.endpoint_url)
            base_path = parsed.path.rstrip("/")
            if self.config.force_path_style:
                path = f"{base_path}/{quote(self.config.bucket, safe='')}"
                netloc = parsed.netloc
            else:
                path = base_path
                netloc = f"{self.config.bucket}.{parsed.netloc}"
            return urlunsplit((parsed.scheme, netloc, path, query_string, ""))

        netloc = f"{self.config.bucket}.s3.{self.config.region}.amazonaws.com"
        return urlunsplit(("https", netloc, "", query_string, ""))

    def _sign(
        self,
        method: str,
        url: str,
        content: bytes,
        headers: dict[str, str],
    ) -> dict[str, str]:
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_scope = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(content).hexdigest()
        parsed = urlsplit(url)

        signed = dict(headers)
        signed["Host"] = parsed.netloc
        signed["x-amz-date"] = amz_date
        signed["x-amz-content-sha256"] = payload_hash

        canonical_headers, signed_header_names = self._canonical_headers(signed)
        canonical_query = self._canonical_query_string_from_raw(parsed.query)
        canonical_request = "\n".join(
            [
                method,
                parsed.path or "/",
                canonical_query,
                canonical_headers,
                signed_header_names,
                payload_hash,
            ]
        )
        credential_scope = f"{date_scope}/{self.config.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(date_scope),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signed["Authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.config.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_header_names}, "
            f"Signature={signature}"
        )
        return signed

    def _canonical_query_string(self, query: dict[str, str]) -> str:
        return urlencode(sorted(query.items()), quote_via=quote, safe="-_.~")

    def _canonical_query_string_from_raw(self, query: str) -> str:
        if not query:
            return ""
        pairs = parse_qsl(query, keep_blank_values=True)
        pairs.sort()
        return "&".join(
            f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}" for name, value in pairs
        )

    def _parse_list_objects_v2(self, content: bytes) -> tuple[list[str], bool, str | None]:
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise S3StorageError(
                "S3 LIST returned invalid XML",
                response_body=content,
                retryable=False,
            ) from exc
        namespace = ""
        if root.tag.startswith("{"):
            namespace = root.tag.partition("}")[0] + "}"

        keys = [
            element.text or "" for element in root.findall(f"{namespace}Contents/{namespace}Key")
        ]
        is_truncated = (root.findtext(f"{namespace}IsTruncated") or "").lower() == "true"
        continuation_token = root.findtext(f"{namespace}NextContinuationToken")
        return keys, is_truncated, continuation_token

    def _canonical_headers(self, headers: dict[str, str]) -> tuple[str, str]:
        canonical = []
        for name, value in headers.items():
            canonical.append((name.lower(), " ".join(str(value).strip().split())))
        canonical.sort()
        canonical_headers = "".join(f"{name}:{value}\n" for name, value in canonical)
        signed_header_names = ";".join(name for name, _ in canonical)
        return canonical_headers, signed_header_names

    def _signing_key(self, date_scope: str) -> bytes:
        key = f"AWS4{self.config.secret_access_key}".encode("utf-8")
        date_key = hmac.new(key, date_scope.encode("utf-8"), hashlib.sha256).digest()
        region_key = hmac.new(
            date_key,
            self.config.region.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()

    def _raise_for_status(
        self,
        method: str,
        response: S3HTTPResponse,
        *,
        allowed: set[int],
    ) -> None:
        if response.status_code in allowed:
            return
        body = response.content.decode("utf-8", errors="replace")
        detail = f": {body}" if body else ""
        raise S3StorageError(
            f"S3 {method} failed with status {response.status_code}{detail}",
            status_code=response.status_code,
            response_body=response.content,
            retryable=response.status_code in self.retry_status_codes,
        )
