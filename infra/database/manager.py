import asyncio
import json
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from infra.logging import get_logger

logger = get_logger(__name__)
DOCUMENT_TABLE = "infra_documents"


DEFAULT_DATABASE_CONFIG: dict[str, Any] = {
    "mysql_enabled": True,
    "mysql_host": "localhost",
    "mysql_port": 3306,
    "mysql_user": "root",
    "mysql_password": "",
    "mysql_db": "test",
    "mysql_pool_minsize": 10,
    "mysql_pool_maxsize": 100,
    "mysql_pool_recycle": 1800,
    "mysql_connect_timeout": 5,
    "redis_enabled": True,
    "redis_url": "redis://localhost:6379/0",
    "redis_max_connections": 200,
    "redis_socket_connect_timeout": 3,
    "redis_socket_timeout": 10,
    "redis_health_check_interval": 30,
    "debug": False,
}


def _load_aiomysql() -> Any:
    try:
        import aiomysql  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "aiomysql is required to use DatabaseManager MySQL features. "
            "Install fastapi-infra[mysql]."
        ) from exc
    return aiomysql


def _load_redis() -> Any:
    try:
        import redis.asyncio as redis
    except ImportError as exc:
        raise RuntimeError(
            "redis is required to use DatabaseManager Redis features. "
            "Install fastapi-infra[redis]."
        ) from exc
    return redis


class DatabaseManager:
    """Explicit MySQL and Redis connection manager."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = {**DEFAULT_DATABASE_CONFIG, **(config or {})}
        self._mysql_pool: Any | None = None
        self._redis_clients: dict[int, Any] = {}
        self._redis_lock = threading.Lock()
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @property
    def mysql_enabled(self) -> bool:
        return bool(self._config.get("mysql_enabled", True))

    @property
    def redis_enabled(self) -> bool:
        return bool(self._config.get("redis_enabled", True))

    async def initialize(self) -> None:
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            try:
                logger.info("Initializing database resources...")

                if self.mysql_enabled:
                    await self._initialize_mysql()

                if self.redis_enabled:
                    await self.get_redis_client()

                self._initialized = True
                logger.info("Database resources initialized")

            except Exception as exc:
                logger.error(f"Database initialization failed: {exc}")
                self._initialized = False
                raise

    async def _initialize_mysql(self) -> None:
        logger.info("Creating MySQL pool...")
        aiomysql = _load_aiomysql()
        self._mysql_pool = await aiomysql.create_pool(
            host=self._config["mysql_host"],
            port=self._config["mysql_port"],
            user=self._config["mysql_user"],
            password=self._config["mysql_password"],
            db=self._config["mysql_db"],
            charset="utf8mb4",
            autocommit=True,
            minsize=self._config["mysql_pool_minsize"],
            maxsize=self._config["mysql_pool_maxsize"],
            pool_recycle=self._config["mysql_pool_recycle"],
            connect_timeout=self._config["mysql_connect_timeout"],
            echo=self._config["debug"],
        )

        logger.info(
            f"MySQL pool created: minsize={self._mysql_pool.minsize}, "
            f"maxsize={self._mysql_pool.maxsize}"
        )

        async with self._mysql_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()
                await cursor.execute("SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci")
                await cursor.execute("SET CHARACTER SET utf8mb4")

    async def close(self) -> None:
        first_error: Exception | None = None

        if self._mysql_pool:
            try:
                logger.info("Closing MySQL pool...")
                self._mysql_pool.close()
                await self._mysql_pool.wait_closed()
                self._mysql_pool = None
            except Exception as exc:
                first_error = exc
                logger.error(f"MySQL pool close failed: {exc}")

        if self._redis_clients:
            logger.info(f"Closing {len(self._redis_clients)} Redis clients...")
            with self._redis_lock:
                clients = dict(self._redis_clients)

            closed_loop_ids: list[int] = []
            for loop_id, client in clients.items():
                try:
                    await client.aclose()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    logger.warning(f"Redis client close failed for loop {loop_id}: {exc}")
                    continue
                closed_loop_ids.append(loop_id)

            with self._redis_lock:
                for loop_id in closed_loop_ids:
                    if self._redis_clients.get(loop_id) is clients[loop_id]:
                        self._redis_clients.pop(loop_id, None)

        if first_error is not None:
            raise first_error

        self._initialized = False
        logger.info("Database resources closed")

    async def health_check(self) -> bool:
        try:
            if self.mysql_enabled:
                if not self._mysql_pool:
                    return False
                async with self._mysql_pool.acquire() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT 1")
                        await cursor.fetchone()

            if self.redis_enabled:
                redis_client = self.get_redis()
                await redis_client.ping()

            return True
        except Exception as exc:
            logger.error(f"Database health check failed: {exc}")
            return False

    @asynccontextmanager
    async def get_connection(self) -> AsyncIterator[Any]:
        if not self.mysql_enabled:
            raise RuntimeError("MySQL is disabled for this DatabaseManager")
        if not self._mysql_pool:
            raise RuntimeError("MySQL pool is not initialized")

        async with self._mysql_pool.acquire() as conn:
            try:
                await conn.ping(reconnect=True)
                yield conn

            except Exception as exc:
                try:
                    await conn.rollback()
                except Exception:
                    pass
                raise exc

    async def acquire_connection(self) -> Any:
        if not self.mysql_enabled:
            raise RuntimeError("MySQL is disabled for this DatabaseManager")
        if not self._mysql_pool:
            raise RuntimeError("MySQL pool is not initialized")
        conn = await self._mysql_pool.acquire()
        await conn.ping(reconnect=True)
        return conn

    async def release_connection(self, conn: Any) -> None:
        if not self.mysql_enabled:
            raise RuntimeError("MySQL is disabled for this DatabaseManager")
        if not self._mysql_pool:
            raise RuntimeError("MySQL pool is not initialized")
        await self._mysql_pool.release(conn)

    async def execute_sql(self, sql: str, params: Any = None, commit: bool = True) -> int:
        async with self.get_connection() as conn, conn.cursor() as cursor:
            await cursor.execute(sql, params)
            if commit:
                await conn.commit()
            return cast(int, cursor.rowcount)

    async def fetch_one(self, sql: str, params: Any = None) -> dict | None:
        aiomysql = _load_aiomysql()
        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(sql, params)
                return cast(dict[Any, Any] | None, await cursor.fetchone())

    async def fetch_all(self, sql: str, params: Any = None) -> list[dict]:
        aiomysql = _load_aiomysql()
        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(sql, params)
                result = await cursor.fetchall()
                return cast(list[dict[Any, Any]], result if result else [])

    async def fetch_many(
        self, sql: str, params: Any = None, size: int | None = None
    ) -> list[dict[Any, Any]]:
        aiomysql = _load_aiomysql()
        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(sql, params)
                result = await cursor.fetchmany(size) if size else await cursor.fetchall()
                return cast(list[dict[Any, Any]], result)

    async def execute_many(self, sql: str, params_list: list[Any], commit: bool = True) -> int:
        async with self.get_connection() as conn, conn.cursor() as cursor:
            await cursor.executemany(sql, params_list)
            if commit:
                await conn.commit()
            return cast(int, cursor.rowcount)

    async def put_document(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        table_name: str = DOCUMENT_TABLE,
    ) -> dict[str, Any]:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        await self.execute_sql(
            f"""
            INSERT INTO {table_name} (collection, document_key, document_value)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE document_value = VALUES(document_value)
            """,
            (collection, key, payload),
        )
        return {"collection": collection, "key": key, "value": dict(value)}

    async def get_document(
        self,
        collection: str,
        key: str,
        *,
        table_name: str = DOCUMENT_TABLE,
    ) -> dict[str, Any] | None:
        row = await self.fetch_one(
            f"""
            SELECT document_value
            FROM {table_name}
            WHERE collection = %s AND document_key = %s
            """,
            (collection, key),
        )
        if row is None:
            return None
        raw_value = row["document_value"]
        value = json.loads(raw_value.decode("utf-8") if isinstance(raw_value, bytes) else raw_value)
        return {"collection": collection, "key": key, "value": value}

    async def delete_document(
        self,
        collection: str,
        key: str,
        *,
        table_name: str = DOCUMENT_TABLE,
    ) -> bool:
        deleted = await self.execute_sql(
            f"""
            DELETE FROM {table_name}
            WHERE collection = %s AND document_key = %s
            """,
            (collection, key),
        )
        return deleted > 0

    async def call_proc(self, proc_name: str, args: list[Any] | None = None) -> Any:
        async with self.get_connection() as conn, conn.cursor() as cursor:
            await cursor.callproc(proc_name, args or [])
            return await cursor.fetchall()

    async def get_redis_client(self) -> Any:
        if not self.redis_enabled:
            raise RuntimeError("Redis is disabled for this DatabaseManager")
        loop_id = self._current_loop_id()

        if loop_id in self._redis_clients:
            return self._redis_clients[loop_id]

        with self._redis_lock:
            if loop_id in self._redis_clients:
                return self._redis_clients[loop_id]

            redis = _load_redis()
            client = redis.from_url(self._config["redis_url"], **self._redis_client_config())
            await client.ping()
            self._redis_clients[loop_id] = client
            logger.info(f"Created Redis client for event loop {loop_id}")

            return client

    def get_redis(self) -> Any:
        loop_id = self._current_loop_id()
        if loop_id not in self._redis_clients:
            if not self.redis_enabled:
                raise RuntimeError("Redis is disabled for this DatabaseManager")
            raise RuntimeError(
                "Redis client for the current event loop is not initialized. "
                "Call initialize() or get_redis_client() first."
            )

        return self._redis_clients[loop_id]

    def _current_loop_id(self) -> int:
        try:
            return id(asyncio.get_running_loop())
        except RuntimeError:
            return -1

    def _redis_client_config(self) -> dict[str, Any]:
        config = {
            "encoding": "utf-8",
            "decode_responses": True,
            "max_connections": self._config["redis_max_connections"],
            "socket_connect_timeout": self._config["redis_socket_connect_timeout"],
            "socket_timeout": self._config["redis_socket_timeout"],
            "socket_keepalive": True,
            "retry_on_timeout": True,
            "retry_on_error": [ConnectionError, TimeoutError],
            "health_check_interval": self._config["redis_health_check_interval"],
        }
        keepalive = self._redis_keepalive_options()
        if keepalive:
            config["socket_keepalive_options"] = keepalive
        return config

    def _redis_keepalive_options(self) -> dict[int, int]:
        if sys.platform == "win32":
            return {}
        try:
            import socket
        except ImportError:
            return {}

        option_names = ("TCP_KEEPIDLE", "TCP_KEEPINTVL", "TCP_KEEPCNT")
        if not all(hasattr(socket, name) for name in option_names):
            return {}
        return {
            getattr(socket, "TCP_KEEPIDLE"): 60,
            getattr(socket, "TCP_KEEPINTVL"): 10,
            getattr(socket, "TCP_KEEPCNT"): 3,
        }

    def get_pool_stats(self) -> dict[str, Any]:
        if not self._mysql_pool:
            return {"initialized": False, "error": "MySQL pool is not initialized"}

        try:
            return {
                "initialized": True,
                "pool_size": self._mysql_pool.size,
                "pool_minsize": self._mysql_pool.minsize,
                "pool_maxsize": self._mysql_pool.maxsize,
                "pool_freesize": self._mysql_pool.freesize,
                "pool_used": self._mysql_pool.size - self._mysql_pool.freesize,
                "pool_usage_percent": (
                    round(
                        (self._mysql_pool.size - self._mysql_pool.freesize)
                        / self._mysql_pool.maxsize
                        * 100,
                        2,
                    )
                    if self._mysql_pool.maxsize > 0
                    else 0
                ),
                "pool_recycle": self._config["mysql_pool_recycle"],
            }
        except Exception as exc:
            return {"initialized": True, "error": str(exc)}
