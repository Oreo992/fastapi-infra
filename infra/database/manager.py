"""
数据库配置和连接管理

高性能异步数据库管理，使用aiomysql + Redis，单例模式确保高并发安全
"""

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Any

import aiomysql
import redis.asyncio as redis

from infra.logging import get_logger


logger = get_logger(__name__)


class DatabaseManager:
    """高性能数据库管理器 - 单例模式，基于aiomysql + Redis
    
    使用方式：
        config = {
            "mysql_host": "localhost",
            "mysql_port": 3306,
            "mysql_user": "root",
            "mysql_password": "",
            "mysql_db": "test",
            "redis_url": "redis://localhost:6379/0",
            "debug": False,
        }
        db = DatabaseManager(config)
        await db.initialize()
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, config: dict = None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config: dict = None):
        if not hasattr(self, "_initialized"):
            self._config = config or {}
            self._mysql_pool: aiomysql.Pool | None = None
            # 支持多事件循环：为每个事件循环维护独立的Redis客户端
            self._redis_clients: dict[int, redis.Redis] = {}
            self._redis_lock = threading.Lock()
            self._initialized = False
            self._init_lock = asyncio.Lock()

    async def initialize(self):
        """初始化数据库连接"""
        # 快速路径：如果已初始化直接返回，避免不必要的锁竞争
        if self._initialized:
            return

        async with self._init_lock:
            # 双重检查锁定模式：再次检查以防其他协程已完成初始化
            if self._initialized:
                return

            try:
                logger.info("初始化高性能数据库连接...")

                # 1. 创建aiomysql连接池 - 优化配置
                # 从配置读取连接池参数
                mysql_minsize = self._config.get("mysql_pool_minsize", 10)
                mysql_maxsize = self._config.get("mysql_pool_maxsize", 100)

                logger.info("创建MySQL连接池...")
                self._mysql_pool = await aiomysql.create_pool(
                    host=self._config.get("mysql_host", "localhost"),
                    port=self._config.get("mysql_port", 3306),
                    user=self._config.get("mysql_user", "root"),
                    password=self._config.get("mysql_password", ""),
                    db=self._config.get("mysql_db", "test"),
                    charset="utf8mb4",
                    autocommit=True,  # 启用autocommit，单条查询自动提交，避免隐式事务导致读延迟
                    minsize=mysql_minsize,
                    maxsize=mysql_maxsize,
                    pool_recycle=self._config.get("mysql_pool_recycle", 1800),
                    connect_timeout=self._config.get("mysql_connect_timeout", 5),
                    echo=self._config.get("debug", False),
                )

                logger.info(
                    f"MySQL连接池已创建: minsize={self._mysql_pool.minsize}, maxsize={self._mysql_pool.maxsize}"
                )

                # 2. 初始化主Redis客户端（主事件循环）
                logger.info("初始化主Redis连接...")
                # 注意：其他事件循环的Redis客户端将在get_redis()中按需创建
                await self._get_or_create_redis_client()

                # 3. 测试MySQL连接并设置字符集
                async with self._mysql_pool.acquire() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT 1")
                        await cursor.fetchone()
                        # 设置连接字符集为utf8mb4 - 确保注释和中文内容正确编码
                        await cursor.execute(
                            "SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci"
                        )
                        await cursor.execute("SET CHARACTER SET utf8mb4")

                # 关键：在锁内最后设置标志，确保完全初始化后才标记为已完成
                self._initialized = True
                logger.info("高性能数据库初始化完成（支持多事件循环Redis）")

            except Exception as e:
                logger.error(f"数据库初始化失败: {e}")
                # 确保初始化失败时标志保持为False，允许重试
                self._initialized = False
                raise

    async def close(self):
        """关闭数据库连接"""
        try:
            if self._mysql_pool:
                logger.info("关闭MySQL连接池...")
                self._mysql_pool.close()
                await self._mysql_pool.wait_closed()
                self._mysql_pool = None

            # 关闭所有事件循环的Redis客户端
            if self._redis_clients:
                logger.info(f"关闭 {len(self._redis_clients)} 个Redis连接...")
                with self._redis_lock:
                    for loop_id, client in self._redis_clients.items():
                        try:
                            await client.aclose()
                        except Exception as e:
                            logger.warning(f"关闭Redis客户端 {loop_id} 失败: {e}")
                    self._redis_clients.clear()

            # 重置初始化标志，允许下次重新初始化
            self._initialized = False

            logger.info("数据库连接已关闭")

        except Exception as e:
            logger.error(f"关闭数据库连接失败: {e}")

    async def health_check(self) -> bool:
        """检查数据库连接健康状态"""
        try:
            if self._mysql_pool:
                async with self._mysql_pool.acquire() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT 1")
                        await cursor.fetchone()

            # 检查当前事件循环的Redis客户端
            redis_client = self.get_redis()
            if redis_client:
                await redis_client.ping()

            return True
        except Exception as e:
            logger.error(f"数据库健康检查失败: {e}")
            return False

    @asynccontextmanager
    async def get_connection(self):
        """获取MySQL连接（带健康检查和事务管理）"""
        if not self._mysql_pool:
            raise RuntimeError("MySQL连接池未初始化")

        async with self._mysql_pool.acquire() as conn:
            try:
                # 检查连接是否有效（ping测试）
                await conn.ping(reconnect=True)
                yield conn

            except Exception as e:
                # 发生异常时回滚(如果有未提交的事务)
                try:
                    await conn.rollback()
                except Exception:
                    pass  # 忽略回滚错误
                raise e

    async def acquire_connection(self):
        """
        从连接池获取连接（显式接口，用于UnitOfWork等需要手动管理连接生命周期的场景）

        Returns:
            数据库连接对象
        """
        if not self._mysql_pool:
            raise RuntimeError("MySQL连接池未初始化")
        conn = await self._mysql_pool.acquire()
        await conn.ping(reconnect=True)
        return conn

    async def release_connection(self, conn):
        """
        释放连接回连接池（显式接口，与acquire_connection配对使用）

        Args:
            conn: 要释放的连接对象
        """
        if not self._mysql_pool:
            raise RuntimeError("MySQL连接池未初始化")
        await self._mysql_pool.release(conn)

    async def execute_sql(
        self, sql: str, params: Any = None, commit: bool = True
    ) -> int:
        """
        执行SQL语句,返回影响的行数

        Args:
            sql: SQL语句
            params: 参数
            commit: 是否提交事务(默认True)

        Returns:
            影响的行数
        """
        async with self.get_connection() as conn, conn.cursor() as cursor:
            await cursor.execute(sql, params)
            if commit:
                await conn.commit()
            return cursor.rowcount

    async def fetch_one(self, sql: str, params: Any = None) -> dict | None:
        """获取单行数据"""
        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(sql, params)
                return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: Any = None) -> list[dict]:
        """获取多行数据"""
        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(sql, params)
                result = await cursor.fetchall()
                return result if result else []

    async def fetch_many(
        self, sql: str, params: Any = None, size: int | None = None
    ) -> list[dict[Any, Any]]:
        """获取指定数量的数据"""
        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(sql, params)
                return await cursor.fetchmany(size) if size else await cursor.fetchall()

    async def execute_many(
        self, sql: str, params_list: list[Any], commit: bool = True
    ) -> int:
        """
        批量执行SQL

        Args:
            sql: SQL语句
            params_list: 参数列表
            commit: 是否提交事务(默认True)

        Returns:
            影响的行数
        """
        async with self.get_connection() as conn, conn.cursor() as cursor:
            await cursor.executemany(sql, params_list)
            if commit:
                await conn.commit()
            return cursor.rowcount

    async def call_proc(self, proc_name: str, args: list[Any] | None = None) -> Any:
        """调用存储过程"""
        async with self.get_connection() as conn, conn.cursor() as cursor:
            await cursor.callproc(proc_name, args or [])
            return await cursor.fetchall()

    async def _get_or_create_redis_client(self) -> redis.Redis:
        """
        获取或创建当前事件循环的Redis客户端

        为每个事件循环维护独立的Redis客户端，避免"Future attached to a different loop"错误
        这对于在工作线程中运行异步任务的场景特别重要
        """
        import sys

        try:
            loop = asyncio.get_running_loop()
            loop_id = id(loop)
        except RuntimeError:
            # 如果没有运行中的事件循环，使用特殊标识
            loop_id = -1

        # 快速路径：如果已存在该循环的客户端，直接返回
        if loop_id in self._redis_clients:
            return self._redis_clients[loop_id]

        # 慢速路径：创建新的客户端（需要加锁）
        with self._redis_lock:
            # 双重检查：其他线程可能已经创建了
            if loop_id in self._redis_clients:
                return self._redis_clients[loop_id]

            # 创建新的Redis客户端配置
            # 从配置读取连接池参数
            redis_config = {
                "encoding": "utf-8",
                "decode_responses": True,
                "max_connections": self._config.get("redis_max_connections", 200),
                "socket_connect_timeout": self._config.get("redis_socket_connect_timeout", 3),
                "socket_timeout": self._config.get("redis_socket_timeout", 10),
                "socket_keepalive": True,
                "retry_on_timeout": True,
                "retry_on_error": [ConnectionError, TimeoutError],
                "health_check_interval": self._config.get("redis_health_check_interval", 30),
            }

            # 仅在Linux/Unix系统上设置socket_keepalive_options
            if sys.platform != "win32":
                try:
                    import socket

                    redis_config["socket_keepalive_options"] = {
                        socket.TCP_KEEPIDLE: 60,
                        socket.TCP_KEEPINTVL: 10,
                        socket.TCP_KEEPCNT: 3,
                    }
                except AttributeError:
                    logger.warning("系统不支持TCP keepalive选项,跳过配置")

            # 创建客户端
            redis_url = self._config.get("redis_url", "redis://localhost:6379/0")
            client = redis.from_url(redis_url, **redis_config)
            await client.ping()

            # 存储客户端
            self._redis_clients[loop_id] = client
            logger.info(
                f"为事件循环 {loop_id} 创建新的Redis客户端（共 {len(self._redis_clients)} 个）"
            )

            return client

    def get_redis(self) -> redis.Redis:
        """
        获取当前事件循环的Redis客户端（同步方法）

        注意：首次调用时会创建客户端，需要在异步上下文中
        建议优先使用 await _get_or_create_redis_client()
        """
        try:
            loop = asyncio.get_running_loop()
            loop_id = id(loop)
        except RuntimeError:
            loop_id = -1

        if loop_id not in self._redis_clients:
            raise RuntimeError(
                "当前事件循环的Redis客户端未初始化。"
                "请先初始化 DatabaseManager 或调用 _get_or_create_redis_client()。"
            )

        return self._redis_clients[loop_id]

    def get_pool_stats(self) -> dict[str, Any]:
        """
        获取连接池统计信息(用于监控)

        Returns:
            连接池状态字典
        """
        if not self._mysql_pool:
            return {"initialized": False, "error": "连接池未初始化"}

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
                "pool_recycle": 1800,  # 连接回收时间(秒)
            }
        except Exception as e:
            return {"initialized": True, "error": str(e)}
