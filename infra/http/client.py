"""
统一HTTP客户端基础层

提供统一的HTTP请求接口，封装aiohttp的底层细节
业务层只需要调用简单的方法，不用关心连接池、会话管理等细节
"""

import asyncio
import inspect
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ContextManager, Protocol, cast
from urllib.parse import urljoin

from infra.logging import get_logger, get_request_id, get_trace_id

logger = get_logger(__name__)
TRACE_ID_HEADER = "X-Trace-ID"
REQUEST_ID_HEADER = "X-Request-ID"


try:
    import orjson as _orjson

    orjson: Any | None = _orjson
except ImportError:  # pragma: no cover - covered by subprocess import guard
    orjson = None


def _load_aiohttp() -> Any:
    try:
        import aiohttp
    except ImportError as exc:
        raise RuntimeError(
            "aiohttp is required to use HttpClient. Install fastapi-infra[http]."
        ) from exc
    return aiohttp


def _orjson_dumps(obj: Any) -> str:
    if orjson is not None:
        return cast(str, orjson.dumps(obj).decode("utf-8"))
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str) -> Any:
    if orjson is not None:
        return orjson.loads(value)
    return json.loads(value)


async def _release_response(response: Any) -> None:
    result = response.release()
    if inspect.isawaitable(result):
        await result


def _has_header(headers: dict[str, str], name: str) -> bool:
    return any(key.lower() == name.lower() for key in headers)


@dataclass
class HttpResponse:
    """HTTP响应封装"""

    status_code: int
    headers: dict[str, str]
    text: str
    content: bytes
    url: str

    @property
    def is_success(self) -> bool:
        """是否成功响应"""
        return 200 <= self.status_code < 300

    def json(self) -> dict[str, Any]:
        """解析JSON响应"""
        try:
            return cast(dict[str, Any], _json_loads(self.text))
        except ValueError as e:
            logger.error(f"JSON解析失败: {e}, 内容: {self.text[:200]}")
            raise

    def raise_for_status(self):
        """如果状态码不成功则抛出异常"""
        if not self.is_success:
            raise HttpError(f"HTTP {self.status_code}: {self.text[:200]}", response=self)


@dataclass(frozen=True)
class HttpRetryConfig:
    """HTTP-specific retry policy.

    The policy is intentionally conservative: only idempotent methods are retried
    unless retry_all_methods is enabled by the caller.
    """

    max_attempts: int = 1
    base_delay: float = 0.25
    retry_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset({429, 500, 502, 503, 504})
    )
    retry_methods: frozenset[str] = field(
        default_factory=lambda: frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
    )
    retry_all_methods: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if isinstance(self.base_delay, bool) or self.base_delay < 0:
            raise ValueError("base_delay must be a non-negative number")


@dataclass
class HttpRequest:
    """HTTP请求配置"""

    method: str
    url: str
    headers: dict[str, str] | None = None
    params: dict[str, Any] | None = None
    data: str | bytes | dict[str, Any] | None = None
    json_data: dict[str, Any] | None = None
    timeout: float | None = None

    def to_kwargs(self) -> dict[str, Any]:
        """转换为aiohttp的请求参数"""
        kwargs: dict[str, Any] = {
            "method": self.method,
            "url": self.url,
            "headers": self.headers or {},
            "params": self.params,
        }

        # 处理请求体
        if self.json_data is not None:
            kwargs["json"] = self.json_data
        elif self.data is not None:
            if isinstance(self.data, dict):
                kwargs["data"] = self.data
            else:
                kwargs["data"] = self.data

        # 设置超时
        if self.timeout:
            kwargs["timeout"] = _load_aiohttp().ClientTimeout(total=self.timeout)

        return kwargs


class HttpError(Exception):
    """HTTP请求异常"""

    def __init__(self, message: str, response: HttpResponse | None = None):
        super().__init__(message)
        self.response = response


class HttpInstrumentation(Protocol):
    def increment(self, name: str, amount: int = 1) -> None: ...

    def timing(self, name: str, value: float) -> None: ...

    def span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> ContextManager[Any]: ...


class HttpClient:
    """
    统一HTTP客户端

    提供简单的HTTP请求接口，封装aiohttp的复杂性
    """

    def __init__(
        self,
        base_url: str = "",
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        retry_config: HttpRetryConfig | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        instrumentation: HttpInstrumentation | None = None,
        propagate_trace_headers: bool = True,
        max_connections: int = 200,
        max_connections_per_host: int = 30,
        keepalive_timeout: int = 60,
    ):
        """
        初始化HTTP客户端

        Args:
            base_url: 基础URL
            timeout: 默认超时时间（秒）
            headers: 默认请求头
            retry_config: 默认 HTTP 重试策略，None 表示不重试
            retry_sleep: 重试等待函数，测试可注入 no-op
            instrumentation: 可选观测服务，记录出站 HTTP 指标和 span
            propagate_trace_headers: 是否自动传播当前 trace/request id 到出站请求头
            max_connections: 最大连接数
            max_connections_per_host: 每个主机的最大连接数
            keepalive_timeout: 连接保活时间（秒）
        """
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.default_timeout = timeout
        self.default_headers = headers or {}
        self.retry_config = retry_config
        self._retry_sleep = retry_sleep
        self._instrumentation = instrumentation
        self._propagate_trace_headers = propagate_trace_headers

        # 连接器配置参数（延迟到事件循环内创建，避免跨循环/跨线程问题）
        self._connector_params: dict[str, Any] = {
            "limit": max_connections,
            "limit_per_host": max_connections_per_host,
            "ttl_dns_cache": 300,
            "use_dns_cache": True,
            "keepalive_timeout": keepalive_timeout,
            "force_close": False,
            "happy_eyeballs_delay": 0.25,
            "family": 0,
        }

        # 为不同事件循环维护独立的会话与连接器
        self._sessions_by_loop: dict[int, Any] = {}
        self._connectors_by_loop: dict[int, Any] = {}
        self._loop_last_used: dict[int, float] = {}  # 记录每个循环最后使用时间

        self._session: Any | None = None

        self._closed = False
        self._request_count = 0
        self._active_connections = 0
        self._cleanup_interval = 300  # 清理间隔(秒) - 5分钟
        self._cleanup_task: asyncio.Task | None = None

    async def _ensure_session(self):
        """确保当前事件循环内的会话已创建，并与该循环绑定。"""
        import time

        loop = asyncio.get_running_loop()
        loop_id = id(loop)

        # 更新最后使用时间
        self._loop_last_used[loop_id] = time.time()

        # 如果该循环下已有会话且未关闭，直接复用
        existing = self._sessions_by_loop.get(loop_id)
        if existing and not existing.closed:
            self._session = existing

            # 启动清理任务(仅启动一次)
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

            return

        # 如存在旧的已关闭会话，清理残留
        if existing and existing.closed:
            self._sessions_by_loop.pop(loop_id, None)
            old_connector = self._connectors_by_loop.pop(loop_id, None)
            if old_connector and not old_connector.closed:
                await old_connector.close()

        # 为当前事件循环创建新的连接器与会话
        aiohttp = _load_aiohttp()
        connector = aiohttp.TCPConnector(**self._connector_params)
        timeout = (
            aiohttp.ClientTimeout(total=self.default_timeout) if self.default_timeout > 0 else None
        )
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=self.default_headers,
            json_serialize=_orjson_dumps,
        )

        self._sessions_by_loop[loop_id] = session
        self._connectors_by_loop[loop_id] = connector
        self._session = session

        # 启动清理任务
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def _periodic_cleanup(self):
        """定期清理不活跃的连接"""
        import time

        while not self._closed:
            try:
                await asyncio.sleep(self._cleanup_interval)

                current_time = time.time()
                stale_loops = []

                # 查找超过2倍清理间隔未使用的循环
                for loop_id, last_used in self._loop_last_used.items():
                    if current_time - last_used > self._cleanup_interval * 2:
                        stale_loops.append(loop_id)

                # 清理不活跃的会话和连接器
                for loop_id in stale_loops:
                    session = self._sessions_by_loop.get(loop_id)
                    connector = self._connectors_by_loop.get(loop_id)

                    try:
                        if session and not session.closed:
                            await session.close()
                            logger.debug(f"清理不活跃的会话: loop_id={loop_id}")

                        if connector and not connector.closed:
                            await connector.close()
                            logger.debug(f"清理不活跃的连接器: loop_id={loop_id}")
                    except Exception as e:
                        logger.warning(f"清理连接失败 loop_id={loop_id}: {e}")
                    finally:
                        self._sessions_by_loop.pop(loop_id, None)
                        self._connectors_by_loop.pop(loop_id, None)
                        self._loop_last_used.pop(loop_id, None)

                if stale_loops:
                    logger.info(f"HTTP客户端定期清理: 清除了 {len(stale_loops)} 个不活跃连接")

            except asyncio.CancelledError:
                logger.debug("HTTP客户端清理任务被取消")
                break
            except Exception as e:
                logger.error(f"HTTP客户端清理任务异常: {e}")

    def _build_url(self, url: str) -> str:
        """构建完整URL"""
        if url.startswith(("http://", "https://")):
            return url
        if self.base_url:
            return urljoin(self.base_url + "/", url.lstrip("/"))
        return url

    def _can_retry_method(self, method: str, retry_config: HttpRetryConfig | None) -> bool:
        if retry_config is None:
            return False
        return retry_config.retry_all_methods or method.upper() in retry_config.retry_methods

    def _should_retry_response(
        self,
        method: str,
        response: HttpResponse,
        retry_config: HttpRetryConfig | None,
        attempt: int,
    ) -> bool:
        if retry_config is None or attempt >= retry_config.max_attempts:
            return False
        if not self._can_retry_method(method, retry_config):
            return False
        return response.status_code in retry_config.retry_status_codes

    def _should_retry_exception(
        self,
        method: str,
        retry_config: HttpRetryConfig | None,
        attempt: int,
    ) -> bool:
        if retry_config is None or attempt >= retry_config.max_attempts:
            return False
        return self._can_retry_method(method, retry_config)

    async def _sleep_before_retry(self, retry_config: HttpRetryConfig, attempt: int) -> None:
        delay = retry_config.base_delay * (2 ** (attempt - 1))
        if delay > 0:
            await self._retry_sleep(delay)

    async def request(self, method: str, url: str, **kwargs) -> HttpResponse:
        """
        发送HTTP请求

        Args:
            method: HTTP方法
            url: 请求URL
            **kwargs: 其他请求参数

        Returns:
            HTTP响应对象
        """
        if self._closed:
            raise HttpError("HTTP客户端已关闭")

        await self._ensure_session()
        session = self._session
        if session is None:
            raise HttpError("HTTP session is not initialized")

        full_url, request_kwargs, retry_config = self._prepare_request_kwargs(url, kwargs)

        attempt = 0
        self._increment_metric("http_client_requests_total")
        while True:
            attempt += 1
            logger.debug(f"HTTP请求: {method} {full_url}")

            self._request_count += 1
            self._increment_metric("http_client_attempts_total")
            start_time = time.monotonic()

            try:
                http_response = await self._send_request_attempt(
                    session,
                    method,
                    full_url,
                    request_kwargs,
                    attempt=attempt,
                    start_time=start_time,
                )
                if self._should_retry_response(method, http_response, retry_config, attempt):
                    assert retry_config is not None
                    self._increment_metric("http_client_retries_total")
                    logger.warning(
                        f"HTTP响应将重试: {method} {full_url} "
                        f"status={http_response.status_code} "
                        f"attempt={attempt}/{retry_config.max_attempts}"
                    )
                    await self._sleep_before_retry(retry_config, attempt)
                    continue

                return http_response

            except TimeoutError as e:
                self._record_error_metrics(method, start_time)
                if self._should_retry_exception(method, retry_config, attempt):
                    assert retry_config is not None
                    self._increment_metric("http_client_retries_total")
                    logger.warning(
                        f"HTTP请求超时后重试: {method} {full_url} "
                        f"attempt={attempt}/{retry_config.max_attempts}"
                    )
                    await self._sleep_before_retry(retry_config, attempt)
                    continue
                logger.error(f"HTTP请求超时: {method} {full_url}, 详细错误: {e}", exc_info=True)
                raise HttpError(f"请求超时: {method} {full_url}") from e
            except _load_aiohttp().ClientError as e:
                self._record_error_metrics(method, start_time)
                if self._should_retry_exception(method, retry_config, attempt):
                    assert retry_config is not None
                    self._increment_metric("http_client_retries_total")
                    logger.warning(
                        f"HTTP请求失败后重试: {method} {full_url} "
                        f"attempt={attempt}/{retry_config.max_attempts} error={e}"
                    )
                    await self._sleep_before_retry(retry_config, attempt)
                    continue
                logger.error(f"HTTP请求失败: {method} {full_url}, 详细错误: {e}", exc_info=True)
                raise HttpError(f"请求失败: {str(e)}") from e
            except Exception as e:
                self._record_error_metrics(method, start_time)
                raise HttpError(f"请求异常: {str(e)}") from e

    async def _send_request_attempt(
        self,
        session: Any,
        method: str,
        full_url: str,
        request_kwargs: dict[str, Any],
        *,
        attempt: int,
        start_time: float,
    ) -> HttpResponse:
        with self._span(
            "http.client.request",
            {
                "http.method": method.upper(),
                "http.url": full_url,
                "http.attempt": attempt,
            },
        ):
            async with session.request(method, full_url, **request_kwargs) as response:
                content = await response.read()
                text = await response.text()

                http_response = HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers),
                    text=text,
                    content=content,
                    url=str(response.url),
                )

                self._record_response_metrics(method, response.status, start_time)
                logger.debug(f"HTTP响应: {response.status} {len(content)} bytes")
                return http_response

    def _prepare_request_kwargs(
        self,
        url: str,
        kwargs: dict[str, Any],
    ) -> tuple[str, dict[str, Any], HttpRetryConfig | None]:
        full_url = self._build_url(url)
        request_kwargs = dict(kwargs)
        request_kwargs["headers"] = self._request_headers(
            request_kwargs.pop("headers", None),
            propagate_trace=self._propagate_trace_headers,
        )
        retry_config = request_kwargs.pop("retry_config", self.retry_config)
        timeout = request_kwargs.pop("timeout", None)
        if timeout is not None:
            request_kwargs["timeout"] = _load_aiohttp().ClientTimeout(total=timeout)
        return full_url, request_kwargs, retry_config

    def _request_headers(
        self,
        headers: dict[str, str] | None = None,
        *,
        propagate_trace: bool,
    ) -> dict[str, str]:
        request_headers = self.default_headers.copy()
        if headers:
            request_headers.update(headers)
        if propagate_trace:
            self._attach_trace_headers(request_headers)
        return request_headers

    def _attach_trace_headers(self, headers: dict[str, str]) -> None:
        trace_id = get_trace_id()
        if trace_id and not _has_header(headers, TRACE_ID_HEADER):
            headers[TRACE_ID_HEADER] = trace_id
        request_id = get_request_id() or trace_id
        if request_id and not _has_header(headers, REQUEST_ID_HEADER):
            headers[REQUEST_ID_HEADER] = request_id

    def _record_response_metrics(
        self,
        method: str,
        status_code: int,
        start_time: float,
    ) -> None:
        self._timing_metric("http_client_request_seconds", time.monotonic() - start_time)
        self._increment_metric("http_client_responses_total")
        self._increment_metric(f"http_client_status_{status_code}_total")
        self._increment_metric(f"http_client_method_{method.upper().lower()}_total")

    def _record_error_metrics(self, method: str, start_time: float) -> None:
        self._timing_metric("http_client_request_seconds", time.monotonic() - start_time)
        self._increment_metric("http_client_errors_total")
        self._increment_metric(f"http_client_method_{method.upper().lower()}_errors_total")

    def _increment_metric(self, name: str, amount: int = 1) -> None:
        if self._instrumentation is not None:
            self._instrumentation.increment(name, amount)

    def _timing_metric(self, name: str, value: float) -> None:
        if self._instrumentation is not None:
            self._instrumentation.timing(name, value)

    def _span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool],
    ) -> ContextManager[Any]:
        if self._instrumentation is None:
            from contextlib import nullcontext

            return nullcontext()
        return self._instrumentation.span(name, attributes)

    async def get(self, url: str, **kwargs) -> HttpResponse:
        """GET请求"""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> HttpResponse:
        """POST请求"""
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> HttpResponse:
        """PUT请求"""
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> HttpResponse:
        """DELETE请求"""
        return await self.request("DELETE", url, **kwargs)

    async def patch(self, url: str, **kwargs) -> HttpResponse:
        """PATCH请求"""
        return await self.request("PATCH", url, **kwargs)

    async def stream_post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """以流式方式发送POST请求，返回异步可迭代对象，每次产出一行文本(去除首尾空白)。

        注意：非200状态将抛出 HttpError 供上层处理。
        """
        if self._closed:
            raise HttpError("HTTP客户端已关闭")

        await self._ensure_session()
        session = self._session
        if session is None:
            raise HttpError("HTTP session is not initialized")

        full_url = self._build_url(url)

        request_headers = self._request_headers(headers, propagate_trace=False)

        try:
            # 计数
            self._request_count += 1

            response = await session.post(full_url, json=json, data=data, headers=request_headers)

            if response.status != 200:
                # 读取部分文本用于错误信息，随后释放连接
                try:
                    error_text = await response.text()
                except Exception:
                    error_text = ""
                finally:
                    await _release_response(response)
                raise HttpError(f"请求失败: HTTP {response.status} - {error_text[:200]}")

            async def _iter():
                try:
                    async for raw_chunk in response.content:
                        if not raw_chunk:
                            continue
                        yield raw_chunk.decode("utf-8", errors="ignore").strip()
                finally:
                    # 确保连接被释放
                    try:
                        await _release_response(response)
                    except Exception:
                        pass

            return _iter()

        except TimeoutError as e:
            logger.error(f"HTTP流式请求超时: POST {full_url}, 详细错误: {e}", exc_info=True)
            raise HttpError(f"请求超时: POST {full_url}") from e
        except _load_aiohttp().ClientError as e:
            logger.error(f"HTTP流式请求失败: POST {full_url}, 详细错误: {e}", exc_info=True)
            raise HttpError(f"请求失败: {str(e)}") from e
        except Exception as e:
            raise HttpError(f"请求异常: {str(e)}") from e

    @asynccontextmanager
    async def session_context(self):
        """会话上下文管理器"""
        await self._ensure_session()
        try:
            yield self
        finally:
            pass  # 保持会话打开，由close()方法关闭

    async def close(self):
        """关闭HTTP客户端（关闭所有事件循环下的会话与连接器）。"""
        self._closed = True  # 先设置关闭标志,停止清理任务

        # 取消清理任务
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        session_count = len(self._sessions_by_loop)
        connector_count = len(self._connectors_by_loop)

        logger.debug(f"开始关闭HTTP客户端: {session_count}个会话, {connector_count}个连接器")

        # 逐个关闭会话
        for loop_id, session in list(self._sessions_by_loop.items()):
            try:
                if session and not session.closed:
                    await session.close()
                    logger.debug(f"已关闭会话 loop_id={loop_id}")
            except Exception as e:
                logger.warning(f"关闭会话失败 loop_id={loop_id}: {e}")
            finally:
                self._sessions_by_loop.pop(loop_id, None)

        # 逐个关闭连接器
        for loop_id, connector in list(self._connectors_by_loop.items()):
            try:
                if connector and not connector.closed:
                    await connector.close()
                    logger.debug(f"已关闭连接器 loop_id={loop_id}")
            except Exception as e:
                logger.warning(f"关闭连接器失败 loop_id={loop_id}: {e}")
            finally:
                self._connectors_by_loop.pop(loop_id, None)

        # 清理最后使用时间记录
        self._loop_last_used.clear()

        # 等待连接器完全关闭
        await asyncio.sleep(0.1)

        self._session = None
        logger.debug("HTTP客户端已关闭")

    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()

    def get_connection_stats(self) -> dict[str, Any]:
        """获取连接池统计信息（针对当前事件循环）。"""
        try:
            loop = asyncio.get_running_loop()
            loop_id = id(loop)
        except RuntimeError:
            # 不在事件循环中
            return {"in_event_loop": False, "total_requests": self._request_count}

        session = self._sessions_by_loop.get(loop_id)
        connector = self._connectors_by_loop.get(loop_id)

        if not session or session.closed or not connector:
            return {
                "in_event_loop": True,
                "session_closed": True,
                "total_requests": self._request_count,
            }

        connector_stats: dict[str, Any] = {}
        if hasattr(connector, "_acquired"):
            connector_stats["acquired_connections"] = len(connector._acquired)
        if hasattr(connector, "_available_connections"):
            connector_stats["available_connections"] = len(connector._available_connections)

        return {
            "in_event_loop": True,
            "session_closed": False,
            "total_requests": self._request_count,
            "connector_limit": connector.limit,
            "connector_limit_per_host": connector.limit_per_host,
            **connector_stats,
        }

    async def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            # 简单的健康检查请求（如果有base_url）
            if self.base_url:
                response = await self.get("/", timeout=5)
                return {
                    "healthy": response.is_success,
                    "status_code": response.status_code,
                    "response_time": "fast",  # 简化处理
                    **self.get_connection_stats(),
                }
            else:
                return {
                    "healthy": not self._closed,
                    "session_active": self._session is not None and not self._session.closed,
                    **self.get_connection_stats(),
                }
        except Exception as e:
            return {"healthy": False, "error": str(e), **self.get_connection_stats()}


class MockHttpClient:
    """Deterministic HTTP client for local development and generated tests."""

    def __init__(
        self,
        *,
        base_url: str = "mock://http",
        status_code: int = 200,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else "mock://http"
        self.status_code = status_code
        self.body = body or {"ok": True}
        self.headers = headers or {"Content-Type": "application/json"}
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        if self.closed:
            raise HttpError("HTTP客户端已关闭")
        full_url = self._build_url(url)
        request = {
            "method": method.upper(),
            "url": full_url,
            "headers": dict(kwargs.get("headers") or {}),
            "params": kwargs.get("params"),
            "json": kwargs.get("json"),
            "data": kwargs.get("data"),
        }
        self.requests.append(request)
        payload = {
            **self.body,
            "request": {
                "method": request["method"],
                "url": request["url"],
            },
        }
        text = _orjson_dumps(payload)
        return HttpResponse(
            status_code=self.status_code,
            headers=dict(self.headers),
            text=text,
            content=text.encode("utf-8"),
            url=full_url,
        )

    def _build_url(self, url: str) -> str:
        if url.startswith(("http://", "https://", "mock://")):
            return url
        if self.base_url.startswith("mock://"):
            return f"{self.base_url}/{url.lstrip('/')}"
        return urljoin(self.base_url + "/", url.lstrip("/"))

    async def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("DELETE", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("PATCH", url, **kwargs)

    async def close(self) -> None:
        self.closed = True

    async def health_check(self) -> dict[str, Any]:
        return {"healthy": not self.closed, "provider": "mock"}
