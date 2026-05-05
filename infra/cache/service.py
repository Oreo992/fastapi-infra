"""
缓存服务 - 基于 Redis

简单易用的 Redis 缓存封装
"""
from typing import Any

import orjson

from infra.database.manager import DatabaseManager
from infra.logging import get_logger


logger = get_logger(__name__)


class CacheService:
    """Redis 缓存服务
    
    使用示例:
        # 创建缓存服务（可选命名空间隔离）
        cache = CacheService(namespace="my_feature")
        
        # 设置缓存（默认 1 小时）
        await cache.set("key", {"data": "value"})
        
        # 获取缓存
        data = await cache.get("key")
        
        # 自定义 TTL
        await cache.set("key", data, ttl=300)  # 5 分钟
        
        # 删除缓存
        await cache.delete("key")
    """

    def __init__(self, namespace: str = "", db_manager: DatabaseManager = None):
        """初始化缓存服务
        
        Args:
            namespace: 命名空间，用于键前缀隔离
            db_manager: DatabaseManager 实例，如果不提供则使用全局单例
        """
        self.namespace = namespace
        self._db_manager = db_manager or DatabaseManager()

    def _make_key(self, key: str) -> str:
        """生成带命名空间的键"""
        return f"{self.namespace}:{key}" if self.namespace else key

    async def get(self, key: str) -> Any:
        """获取缓存
        
        Args:
            key: 缓存键
            
        Returns:
            缓存值，不存在返回 None
        """
        redis = await self._db_manager._get_or_create_redis_client()
        full_key = self._make_key(key)
        
        try:
            value = await redis.get(full_key)
            if value:
                try:
                    # 尝试 JSON 反序列化
                    return orjson.loads(value)
                except Exception:
                    # 如果不是 JSON，直接返回字符串
                    return value
            return None
        except Exception as e:
            logger.error(f"获取缓存失败: {full_key}, 错误: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: int = 3600) -> bool:
        """设置缓存
        
        Args:
            key: 缓存键
            value: 缓存值（自动 JSON 序列化）
            ttl: 过期时间（秒），默认 1 小时
            
        Returns:
            成功返回 True，失败返回 False
        """
        redis = await self._db_manager._get_or_create_redis_client()
        full_key = self._make_key(key)
        
        try:
            # JSON 序列化
            serialized = orjson.dumps(value)
            await redis.setex(full_key, ttl, serialized)
            return True
        except Exception as e:
            logger.error(f"设置缓存失败: {full_key}, 错误: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """删除缓存
        
        Args:
            key: 缓存键
            
        Returns:
            成功返回 True，失败返回 False
        """
        redis = await self._db_manager._get_or_create_redis_client()
        full_key = self._make_key(key)
        
        try:
            result = await redis.delete(full_key)
            return result > 0
        except Exception as e:
            logger.error(f"删除缓存失败: {full_key}, 错误: {e}")
            return False

    async def exists(self, key: str) -> bool:
        """检查缓存是否存在
        
        Args:
            key: 缓存键
            
        Returns:
            存在返回 True，不存在返回 False
        """
        redis = await self._db_manager._get_or_create_redis_client()
        full_key = self._make_key(key)
        
        try:
            result = await redis.exists(full_key)
            return result > 0
        except Exception as e:
            logger.error(f"检查缓存失败: {full_key}, 错误: {e}")
            return False

    async def expire(self, key: str, ttl: int) -> bool:
        """设置缓存过期时间
        
        Args:
            key: 缓存键
            ttl: 过期时间（秒）
            
        Returns:
            成功返回 True，失败返回 False
        """
        redis = await self._db_manager._get_or_create_redis_client()
        full_key = self._make_key(key)
        
        try:
            result = await redis.expire(full_key, ttl)
            return result
        except Exception as e:
            logger.error(f"设置过期时间失败: {full_key}, 错误: {e}")
            return False
