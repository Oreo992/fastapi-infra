import pytest

from infra.plugins.lock.manager import DistributedLockManager, LockAcquisitionError


class FakeDatabaseManager:
    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def get_redis_client(self) -> "FakeRedis":
        return self.redis


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiry: dict[str, int] = {}

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expiry[key] = ex
        return True

    async def eval(self, script: str, count: int, key: str, token: str, *args: int) -> int:
        assert count == 1
        if self.values.get(key) != token:
            return 0
        if "expire" in script:
            self.expiry[key] = int(args[0])
            return 1
        self.values.pop(key, None)
        self.expiry.pop(key, None)
        return 1


async def test_lock_manager_acquires_lock_with_namespaced_key() -> None:
    redis = FakeRedis()
    manager = DistributedLockManager(FakeDatabaseManager(redis), prefix="svc")

    token = await manager.acquire("job:1", timeout=30, blocking=False)

    assert redis.values == {"svc:job:1": token}
    assert redis.expiry == {"svc:job:1": 30}


async def test_lock_manager_non_blocking_acquire_fails_when_lock_exists() -> None:
    redis = FakeRedis()
    redis.values["lock:job:1"] = "other-token"
    manager = DistributedLockManager(FakeDatabaseManager(redis))

    with pytest.raises(LockAcquisitionError, match="无法获取锁"):
        await manager.acquire("job:1", blocking=False)


async def test_lock_manager_blocking_acquire_respects_zero_block_timeout() -> None:
    redis = FakeRedis()
    redis.values["lock:job:1"] = "other-token"
    manager = DistributedLockManager(FakeDatabaseManager(redis))

    with pytest.raises(LockAcquisitionError, match="获取锁超时"):
        await manager.acquire("job:1", blocking=True, block_timeout=0)


async def test_lock_manager_releases_only_matching_token() -> None:
    redis = FakeRedis()
    manager = DistributedLockManager(FakeDatabaseManager(redis))
    token = await manager.acquire("job:1")

    assert await manager.release("job:1", "wrong-token") is False
    assert redis.values["lock:job:1"] == token

    assert await manager.release("job:1", token) is True
    assert "lock:job:1" not in redis.values


async def test_lock_manager_extends_only_matching_token() -> None:
    redis = FakeRedis()
    manager = DistributedLockManager(FakeDatabaseManager(redis))
    token = await manager.acquire("job:1", timeout=10)

    assert await manager.extend("job:1", "wrong-token", ttl=60) is False
    assert redis.expiry["lock:job:1"] == 10

    assert await manager.extend("job:1", token, ttl=60) is True
    assert redis.expiry["lock:job:1"] == 60


async def test_lock_manager_context_manager_releases_after_error() -> None:
    redis = FakeRedis()
    manager = DistributedLockManager(FakeDatabaseManager(redis))

    with pytest.raises(RuntimeError, match="boom"):
        async with manager.lock("job:1"):
            assert "lock:job:1" in redis.values
            raise RuntimeError("boom")

    assert "lock:job:1" not in redis.values


async def test_lock_manager_validates_inputs() -> None:
    manager = DistributedLockManager(FakeDatabaseManager(FakeRedis()))

    with pytest.raises(ValueError, match="lock key"):
        await manager.acquire(" ")

    with pytest.raises(ValueError, match="timeout"):
        await manager.acquire("job:1", timeout=0)

    with pytest.raises(ValueError, match="block_timeout"):
        await manager.acquire("job:1", block_timeout=-1)

    with pytest.raises(ValueError, match="token"):
        await manager.release("job:1", "")

    with pytest.raises(ValueError, match="ttl"):
        await manager.extend("job:1", "token", ttl=0)
