from app.core.cache import MemoryTTLCache
from app.core.rate_limit import MemoryRateLimiter


def test_memory_cache_expires_without_external_service() -> None:
    now = [100.0]
    cache = MemoryTTLCache(ttl_seconds=5, clock=lambda: now[0])
    cache.set("key", {"value": 1})
    assert cache.get("key") == {"value": 1}
    now[0] = 106.0
    assert cache.get("key") is None


def test_memory_rate_limiter_blocks_after_limit() -> None:
    limiter = MemoryRateLimiter(2, 60, clock=lambda: 10.0)
    assert limiter.check("tenant").allowed
    assert limiter.check("tenant").allowed
    blocked = limiter.check("tenant")
    assert not blocked.allowed
    assert blocked.retry_after == 60
