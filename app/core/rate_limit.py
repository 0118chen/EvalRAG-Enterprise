"""Fixed-window rate limiting with memory and Redis backends."""

import logging
import time
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class RateLimiter(Protocol):
    def check(self, key: str, now: float | None = None) -> RateLimitDecision: ...


class MemoryRateLimiter:
    def __init__(self, limit: int, window_seconds: int, clock=time.monotonic) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._windows: dict[str, tuple[float, int]] = {}

    def check(self, key: str, now: float | None = None) -> RateLimitDecision:
        current = self.clock() if now is None else now
        window_start, count = self._windows.get(key, (current, 0))
        if current - window_start >= self.window_seconds:
            window_start, count = current, 0
        count += 1
        self._windows[key] = (window_start, count)
        retry_after = max(1, int(self.window_seconds - (current - window_start)))
        return RateLimitDecision(
            allowed=count <= self.limit,
            limit=self.limit,
            remaining=max(0, self.limit - count),
            retry_after=retry_after,
        )


class RedisRateLimiter:
    def __init__(self, url: str, limit: int, window_seconds: int) -> None:
        import redis

        self.limit = limit
        self.window_seconds = window_seconds
        self.client = redis.Redis.from_url(url, decode_responses=True)

    def check(self, key: str, now: float | None = None) -> RateLimitDecision:
        redis_key = f"evalrag:rate:{key}"
        try:
            pipeline = self.client.pipeline()
            pipeline.incr(redis_key)
            pipeline.expire(redis_key, self.window_seconds, nx=True)
            pipeline.ttl(redis_key)
            count, _, ttl = pipeline.execute()
            retry_after = max(1, int(ttl if ttl and ttl > 0 else self.window_seconds))
            return RateLimitDecision(
                allowed=int(count) <= self.limit,
                limit=self.limit,
                remaining=max(0, self.limit - int(count)),
                retry_after=retry_after,
            )
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            return RateLimitDecision(
                allowed=True,
                limit=self.limit,
                remaining=self.limit,
                retry_after=0,
            )


def create_rate_limiter(settings: Settings) -> RateLimiter | None:
    if not settings.rate_limit_enabled:
        return None
    if settings.rate_limit_backend == "redis":
        try:
            return RedisRateLimiter(
                settings.redis_url,
                settings.rate_limit_requests,
                settings.rate_limit_window_seconds,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Redis rate limiter initialization failed: %s", exc)
    return MemoryRateLimiter(
        settings.rate_limit_requests,
        settings.rate_limit_window_seconds,
    )
