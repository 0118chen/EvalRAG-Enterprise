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


class NullCache:
    async def get(self, key: str) -> Any | None:
        return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        return None


@dataclass
class MemoryTTLCache:
    ttl_seconds: int = 120
    clock: Any = time.monotonic
    _items: dict[str, tuple[float, Any]] = field(default_factory=dict)

    async def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at <= self.clock():
            self._items.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._items[key] = (self.clock() + ttl, value)


@dataclass
class RedisTTLCache:
    url: str
    prefix: str = "evalrag:"
    ttl_seconds: int = 120

    def __post_init__(self) -> None:
        import redis.asyncio as redis_asyncio

        self.client = redis_asyncio.Redis.from_url(self.url, decode_responses=True)

    async def get(self, key: str) -> Any | None:
        try:
            payload = await self.client.get(f"{self.prefix}{key}")
        except REDIS_FAILURES as exc:
            logger.warning("Redis cache unavailable, treating key as a miss: %s", exc)
            return None
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("discarding malformed cache entry for %s", key)
            return None

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


def create_cache(settings: Settings) -> Cache:
    if not settings.cache_enabled or settings.cache_backend == "none":
        return NullCache()
    if settings.cache_backend == "redis":
        try:
            return RedisTTLCache(settings.redis_url, ttl_seconds=settings.cache_ttl_seconds)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Redis cache initialization failed: %s", exc)
    return MemoryTTLCache(settings.cache_ttl_seconds)


def cache_key(namespace: str, payload: dict[str, Any]) -> str:
    return f"{namespace}:{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
