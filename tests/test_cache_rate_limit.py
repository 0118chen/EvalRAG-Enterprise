import asyncio

from app.core.cache import MemoryTTLCache
from app.core.rate_limit import MemoryRateLimiter


async def _expire_cache(cache: MemoryTTLCache, now: list[float]):
    await cache.set("key", {"value": 1})
    assert await cache.get("key") == {"value": 1}
    now[0] = 106.0
    return await cache.get("key")


def test_memory_cache_expires_without_external_service() -> None:
    now = [100.0]
    cache = MemoryTTLCache(ttl_seconds=5, clock=lambda: now[0])

    assert asyncio.run(_expire_cache(cache, now)) is None


def test_memory_rate_limiter_blocks_after_limit() -> None:
    async def exercise():
        limiter = MemoryRateLimiter(2, 60, clock=lambda: 10.0)
        return [await limiter.check("tenant") for _ in range(3)]

    allowed, allowed_again, blocked = asyncio.run(exercise())

    assert allowed.allowed
    assert allowed_again.allowed
    assert not blocked.allowed
    assert blocked.retry_after == 60
