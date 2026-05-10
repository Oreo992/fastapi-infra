"""分布式锁管理器

基于Redis实现的分布式锁，支持：
- 非阻塞/阻塞获取
- 自动过期（防止死锁）
- Lua脚本原子释放
- 上下文管理器模式
"""

import asyncio
import uuid
from contextlib import asynccontextmanager

from infra.database.manager import DatabaseManager
from infra.logging import get_logger

logger = get_logger(__name__)


class LockAcquisitionError(Exception):
    """锁获取失败异常"""

    pass


class DistributedLockManager:
    """基于Redis的分布式锁管理器"""

    def __init__(self, db: DatabaseManager, prefix: str = "lock"):
        """初始化锁管理器

        Args:
            prefix: 锁key的前缀，用于命名空间隔离
        """
        self.prefix = prefix
        self._db = db
        self._lock_tokens = {}  # {key: token} 记录已获取的锁

    def _build_key(self, key: str) -> str:
        """构建完整的锁key

        Args:
            key: 业务锁key

        Returns:
            完整的Redis key
        """
        return f"{self.prefix}:{key}"

    async def acquire(
        self,
        key: str,
        timeout: int = 300,
        blocking: bool = True,
        block_timeout: int = 10,
    ) -> str:
        """获取锁

        Args:
            key: 锁的键
            timeout: 锁的超时时间（秒），防止死锁
            blocking: 是否阻塞等待
            block_timeout: 阻塞等待的最大时间（秒）

        Returns:
            锁的token（用于释放）

        Raises:
            LockAcquisitionError: 获取锁失败
        """
        redis = await self._get_redis()
        lock_key = self._build_key(key)
        token = str(uuid.uuid4())

        start_time = asyncio.get_event_loop().time()

        while True:
            # 尝试获取锁（SET NX EX）
            acquired = await redis.set(
                lock_key,
                token,
                nx=True,  # 只在键不存在时设置
                ex=timeout,  # 过期时间
            )

            if acquired:
                self._lock_tokens[key] = token
                logger.info(f"获取锁成功: {key}, token={token[:8]}..., ttl={timeout}s")
                return token

            if not blocking:
                raise LockAcquisitionError(f"无法获取锁: {key}（已被占用）")

            # 检查阻塞超时
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= block_timeout:
                raise LockAcquisitionError(
                    f"获取锁超时: {key}（等待{block_timeout}秒后仍被占用）"
                )

            # 等待后重试
            await asyncio.sleep(0.1)

    async def release(self, key: str, token: str) -> bool:
        """释放锁（使用Lua脚本保证原子性）

        Args:
            key: 锁的键
            token: 锁的token（必须匹配才能释放）

        Returns:
            是否成功释放
        """
        redis = await self._get_redis()
        lock_key = self._build_key(key)

        # Lua脚本：只有token匹配才删除
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """

        result = await redis.eval(lua_script, 1, lock_key, token)

        if result:
            self._lock_tokens.pop(key, None)
            logger.info(f"释放锁成功: {key}")
            return True
        else:
            logger.warning(f"释放锁失败: {key}（token不匹配或已过期）")
            return False

    async def extend(self, key: str, token: str, ttl: int) -> bool:
        """延长锁的过期时间

        Args:
            key: 锁的键
            token: 锁的token（必须匹配才能延期）
            ttl: 新的过期时间（秒）

        Returns:
            是否成功延期
        """
        redis = await self._get_redis()
        lock_key = self._build_key(key)

        # Lua脚本：只有token匹配才延期
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """

        result = await redis.eval(lua_script, 1, lock_key, token, ttl)
        if result:
            logger.info(f"延长锁成功: {key}, 新TTL={ttl}s")
        return bool(result)

    @asynccontextmanager
    async def lock(
        self, key: str, timeout: int = 300, blocking: bool = True, block_timeout: int = 10
    ):
        """上下文管理器方式使用锁

        Args:
            key: 锁的键
            timeout: 锁的超时时间（秒）
            blocking: 是否阻塞等待
            block_timeout: 阻塞等待的最大时间（秒）

        Yields:
            锁的token

        Example:
            async with lock_manager.lock("my_resource", timeout=60):
                # 执行需要加锁的操作
                pass
        """
        token = None
        try:
            token = await self.acquire(key, timeout, blocking, block_timeout)
            yield token
        finally:
            if token:
                await self.release(key, token)

    async def _get_redis(self):
        await self._db.initialize()
        return await self._db._get_or_create_redis_client()
