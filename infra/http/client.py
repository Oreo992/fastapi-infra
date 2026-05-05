"""
统一HTTP客户端基础层

提供统一的HTTP请求接口，封装aiohttp的底层细节
业务层只需要调用简单的方法，不用关心连接池、会话管理等细节
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import aiohttp
import orjson
from aiohttp import ClientSession, ClientTimeout

from infra.logging import get_logger


logger = get_logger(__name__)


def _orjson_dumps(obj: Any) -> str:
    return orjson.dumps(obj).decode("utf-8")


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
            return orjson.loads(self.text)
        except orjson.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}, 内容: {self.text[:200]}")
            raise

    def raise_for_status(self):
        """如果状态码不成功则抛出异常"""
        if not self.is_success:
            raise HttpError(f"HTTP {self.status_code}: {self.text[:200]}")


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
        kwargs = {
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
            kwargs["timeout"] = ClientTimeout(total=self.timeout)

        return kwargs


class HttpError(Exception):
    """HTTP请求异常"""

    def __init__(self, message: str, response: HttpResponse | None = None):
        super().__init__(message)
        self.response = response


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
            max_connections: 最大连接数
            max_connections_per_host: 每个主机的最大连接数
            keepalive_timeout: 连接保活时间（秒）
        """
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.default_timeout = timeout
        self.default_headers = headers or {}

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
        self._sessions_by_loop: dict[int, ClientSession] = {}
        self._connectors_by_loop: dict[int, aiohttp.TCPConnector] = {}
        self._loop_last_used: dict[int, float] = {}  # 记录每个循环最后使用时间

        # 兼容旧字段，指向当前循环对应的会话（仅用于便捷访问）
        self._session: ClientSession | None = None

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
        connector = aiohttp.TCPConnector(**self._connector_params)
        timeout = (
            ClientTimeout(total=self.default_timeout)
            if self.default_timeout > 0
            else None
        )
        session = ClientSession(
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
                    logger.info(
                        f"HTTP客户端定期清理: 清除了 {len(stale_loops)} 个不活跃连接"
                    )

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

        # 构建完整URL
        full_url = self._build_url(url)

        # 合并请求头
        headers = self.default_headers.copy()
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers", {}))

        # 移除timeout参数避免冲突
        if "timeout" in kwargs:
            kwargs.pop("timeout")

        try:
            logger.debug(f"HTTP请求: {method} {full_url}")

            # 增加请求计数
            self._request_count += 1

            async with self._session.request(
                method, full_url, headers=headers, **kwargs
            ) as response:
                # 读取响应内容
                content = await response.read()
                text = await response.text()

                http_response = HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers),
                    text=text,
                    content=content,
                    url=str(response.url),
                )

                logger.debug(f"HTTP响应: {response.status} {len(content)} bytes")

                return http_response

        except TimeoutError as e:
            logger.error(
                f"HTTP请求超时: {method} {full_url}, 详细错误: {e}", exc_info=True
            )
            raise HttpError(f"请求超时: {method} {full_url}") from e
        except aiohttp.ClientError as e:
            logger.error(
                f"HTTP请求失败: {method} {full_url}, 详细错误: {e}", exc_info=True
            )
            raise HttpError(f"请求失败: {str(e)}") from e
        except Exception as e:
            raise HttpError(f"请求异常: {str(e)}") from e

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

        full_url = self._build_url(url)

        request_headers = self.default_headers.copy()
        if headers:
            request_headers.update(headers)

        try:
            # 计数
            self._request_count += 1

            response = await self._session.post(
                full_url, json=json, data=data, headers=request_headers
            )

            if response.status != 200:
                # 读取部分文本用于错误信息，随后释放连接
                try:
                    error_text = await response.text()
                except Exception:
                    error_text = ""
                finally:
                    await response.release()
                raise HttpError(
                    f"请求失败: HTTP {response.status} - {error_text[:200]}"
                )

            async def _iter():
                try:
                    async for raw_chunk in response.content:
                        if not raw_chunk:
                            continue
                        yield raw_chunk.decode("utf-8", errors="ignore").strip()
                finally:
                    # 确保连接被释放
                    try:
                        await response.release()
                    except Exception:
                        pass

            return _iter()

        except TimeoutError as e:
            logger.error(
                f"HTTP流式请求超时: POST {full_url}, 详细错误: {e}", exc_info=True
            )
            raise HttpError(f"请求超时: POST {full_url}") from e
        except aiohttp.ClientError as e:
            logger.error(
                f"HTTP流式请求失败: POST {full_url}, 详细错误: {e}", exc_info=True
            )
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

        logger.debug(
            f"开始关闭HTTP客户端: {session_count}个会话, {connector_count}个连接器"
        )

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
            connector_stats["available_connections"] = len(
                connector._available_connections
            )

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
                    "session_active": self._session is not None
                    and not self._session.closed,
                    **self.get_connection_stats(),
                }
        except Exception as e:
            return {"healthy": False, "error": str(e), **self.get_connection_stats()}


class HttpClientManager:
    """
    HTTP客户端管理器

    管理全局的HTTP客户端实例，提供单例和工厂模式
    """

    _instances: dict[str, HttpClient] = {}

    @classmethod
    def get_client(
        cls, name: str = "default", base_url: str = "", **kwargs
    ) -> HttpClient:
        """
        获取HTTP客户端实例

        Args:
            name: 客户端名称，用于区分不同的客户端
            base_url: 基础URL
            **kwargs: 客户端配置参数

        Returns:
            HTTP客户端实例
        """
        if name not in cls._instances:
            cls._instances[name] = HttpClient(base_url=base_url, **kwargs)
        return cls._instances[name]

    @classmethod
    async def close_all(cls):
        """关闭所有客户端"""
        for client in cls._instances.values():
            await client.close()
        cls._instances.clear()
        logger.info("所有HTTP客户端已关闭")

    @classmethod
    def get_all_stats(cls) -> dict[str, dict[str, Any]]:
        """获取所有客户端的统计信息"""
        stats = {}
        for name, client in cls._instances.items():
            stats[name] = client.get_connection_stats()
        return stats

    @classmethod
    async def health_check_all(cls) -> dict[str, dict[str, Any]]:
        """对所有客户端进行健康检查"""
        health_status = {}
        for name, client in cls._instances.items():
            try:
                health_status[name] = await client.health_check()
            except Exception as e:
                health_status[name] = {"healthy": False, "error": str(e)}
        return health_status

    @classmethod
    def list_clients(cls) -> list[str]:
        """列出所有客户端名称"""
        return list(cls._instances.keys())


# 便捷函数
async def get(url: str, **kwargs) -> HttpResponse:
    """便捷GET请求"""
    client = HttpClientManager.get_client()
    return await client.get(url, **kwargs)


async def post(url: str, **kwargs) -> HttpResponse:
    """便捷POST请求"""
    client = HttpClientManager.get_client()
    return await client.post(url, **kwargs)


async def put(url: str, **kwargs) -> HttpResponse:
    """便捷PUT请求"""
    client = HttpClientManager.get_client()
    return await client.put(url, **kwargs)


async def delete(url: str, **kwargs) -> HttpResponse:
    """便捷DELETE请求"""
    client = HttpClientManager.get_client()
    return await client.delete(url, **kwargs)
