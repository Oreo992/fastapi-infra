import json
import time
from typing import Any, cast

from infra.database.manager import DatabaseManager
from infra.logging import get_logger

logger = get_logger(__name__)


try:
    import orjson as _orjson

    orjson: Any | None = _orjson
except ImportError:  # pragma: no cover - covered by subprocess import guard
    orjson = None


def _json_loads(value: str | bytes) -> Any:
    if orjson is not None:
        return orjson.loads(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)


def _json_dumps(value: Any) -> bytes:
    if orjson is not None:
        return cast(bytes, orjson.dumps(value))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class CacheService:
    """Small Redis cache facade backed by an explicit DatabaseManager."""

    def __init__(self, namespace: str, db_manager: DatabaseManager):
        self.namespace = namespace
        self._db_manager = db_manager

    def _make_key(self, key: str) -> str:
        return f"{self.namespace}:{key}" if self.namespace else key

    async def health_check(self) -> bool:
        try:
            redis = await self._db_manager.get_redis_client()
            return bool(await redis.ping())
        except Exception as exc:
            logger.error(f"Cache health check failed: {exc}")
            return False

    async def get(self, key: str) -> Any:
        redis = await self._db_manager.get_redis_client()
        full_key = self._make_key(key)

        try:
            value = await redis.get(full_key)
            if value:
                try:
                    return _json_loads(value)
                except Exception:
                    return value
            return None
        except Exception as exc:
            logger.error(f"Cache get failed for {full_key}: {exc}")
            return None

    async def set(self, key: str, value: Any, ttl: int = 3600) -> bool:
        redis = await self._db_manager.get_redis_client()
        full_key = self._make_key(key)

        try:
            serialized = _json_dumps(value)
            await redis.setex(full_key, ttl, serialized)
            return True
        except Exception as exc:
            logger.error(f"Cache set failed for {full_key}: {exc}")
            return False

    async def delete(self, key: str) -> bool:
        redis = await self._db_manager.get_redis_client()
        full_key = self._make_key(key)

        try:
            result = await redis.delete(full_key)
            return cast(bool, result > 0)
        except Exception as exc:
            logger.error(f"Cache delete failed for {full_key}: {exc}")
            return False

    async def exists(self, key: str) -> bool:
        redis = await self._db_manager.get_redis_client()
        full_key = self._make_key(key)

        try:
            result = await redis.exists(full_key)
            return cast(bool, result > 0)
        except Exception as exc:
            logger.error(f"Cache exists failed for {full_key}: {exc}")
            return False

    async def expire(self, key: str, ttl: int) -> bool:
        redis = await self._db_manager.get_redis_client()
        full_key = self._make_key(key)

        try:
            result = await redis.expire(full_key, ttl)
            return cast(bool, result)
        except Exception as exc:
            logger.error(f"Cache expire failed for {full_key}: {exc}")
            return False


class MemoryCacheService:
    """In-process cache for local development and tests."""

    def __init__(self, namespace: str = "") -> None:
        self.namespace = namespace
        self._items: dict[str, tuple[Any, float | None]] = {}

    def _make_key(self, key: str) -> str:
        return f"{self.namespace}:{key}" if self.namespace else key

    async def health_check(self) -> bool:
        return True

    async def get(self, key: str) -> Any:
        full_key = self._make_key(key)
        item = self._items.get(full_key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at is not None and expires_at <= time.monotonic():
            self._items.pop(full_key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl: int = 3600) -> bool:
        full_key = self._make_key(key)
        expires_at = time.monotonic() + ttl if ttl > 0 else None
        self._items[full_key] = (value, expires_at)
        return True

    async def delete(self, key: str) -> bool:
        full_key = self._make_key(key)
        return self._items.pop(full_key, None) is not None

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    async def expire(self, key: str, ttl: int) -> bool:
        value = await self.get(key)
        if value is None:
            return False
        full_key = self._make_key(key)
        expires_at = time.monotonic() + ttl if ttl > 0 else None
        self._items[full_key] = (value, expires_at)
        return True
