"""Small cache abstraction with memory, Redis and no-op implementations.

Every backend is asynchronous because the cache is read from async request
handlers: a synchronous Redis client would block the event loop on each lookup.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings
from app.core.metrics import Metrics

logger = logging.getLogger(__name__)

REDIS_FAILURES: tuple[type[BaseException], ...]
try:  # keep the memory and null backends usable without the redis package
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover - redis is a declared dependency
    REDIS_FAILURES = (OSError, RuntimeError, ValueError)
else:
    # redis-py raises its own ConnectionError/TimeoutError subclasses, which are not the
    # builtin ones. Catching the library base class is what makes a Redis outage degrade
    # instead of surfacing as a 500.
    REDIS_FAILURES = (RedisError, OSError, RuntimeError, ValueError)


class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...


def _record(
    metrics: Metrics | None,
    backend: str,
    outcome: str,
) -> None:
    """Bump one cache counter, if this cache was built with metrics attached."""
    if metrics is None:
        return
    if outcome == "hit":
        metrics.observe_cache_hit(backend)
    elif outcome == "miss":
        metrics.observe_cache_miss(backend)
    else:
        metrics.observe_cache_error(backend)


class NullCache:
    async def get(self, key: str) -> Any | None:
        return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        return None


@dataclass
class MemoryTTLCache:
    ttl_seconds: int = 120
    clock: Any = time.monotonic
    metrics: Metrics | None = None
    _items: dict[str, tuple[float, Any]] = field(default_factory=dict)

    async def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if not item:
            _record(self.metrics, "memory", "miss")
            return None
        expires_at, value = item
        if expires_at <= self.clock():
            self._items.pop(key, None)
            _record(self.metrics, "memory", "miss")
            return None
        _record(self.metrics, "memory", "hit")
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._items[key] = (self.clock() + ttl, value)


@dataclass
class RedisTTLCache:
    url: str
    prefix: str = "evalrag:"
    ttl_seconds: int = 120
    metrics: Metrics | None = None

    def __post_init__(self) -> None:
        import redis.asyncio as redis_asyncio

        self.client = redis_asyncio.Redis.from_url(self.url, decode_responses=True)

    async def get(self, key: str) -> Any | None:
        try:
            payload = await self.client.get(f"{self.prefix}{key}")
        except REDIS_FAILURES as exc:
            # An outage is not a lookup answer: it is counted separately from a miss so a
            # hit ratio cannot be quietly dragged down by Redis being unreachable.
            logger.warning("Redis cache unavailable, treating key as a miss: %s", exc)
            _record(self.metrics, "redis", "error")
            return None
        if not payload:
            _record(self.metrics, "redis", "miss")
            return None
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("discarding malformed cache entry for %s", key)
            _record(self.metrics, "redis", "error")
            return None
        _record(self.metrics, "redis", "hit")
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        try:
            await self.client.set(
                f"{self.prefix}{key}",
                json.dumps(value, ensure_ascii=False),
                ex=ttl,
            )
        except REDIS_FAILURES as exc:
            logger.warning("Redis cache unavailable, skipping cache write: %s", exc)
            _record(self.metrics, "redis", "error")


def create_cache(settings: Settings, metrics: Metrics | None = None) -> Cache:
    if not settings.cache_enabled or settings.cache_backend == "none":
        return NullCache()
    if settings.cache_backend == "redis":
        try:
            return RedisTTLCache(
                settings.redis_url,
                ttl_seconds=settings.cache_ttl_seconds,
                metrics=metrics,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Redis cache initialization failed: %s", exc)
    return MemoryTTLCache(settings.cache_ttl_seconds, metrics=metrics)


def cache_key(namespace: str, payload: dict[str, Any]) -> str:
    return f"{namespace}:{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
