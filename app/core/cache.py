"""Small cache abstraction with memory, Redis and no-op implementations."""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings

logger = logging.getLogger(__name__)


class Cache(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...


class NullCache:
    def get(self, key: str) -> Any | None:
        return None

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        return None


@dataclass
class MemoryTTLCache:
    ttl_seconds: int = 120
    clock: Any = time.monotonic
    _items: dict[str, tuple[float, Any]] = field(default_factory=dict)

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at <= self.clock():
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._items[key] = (self.clock() + ttl, value)


@dataclass
class RedisTTLCache:
    url: str
    prefix: str = "evalrag:"
    ttl_seconds: int = 120

    def __post_init__(self) -> None:
        import redis

        self.client = redis.Redis.from_url(self.url, decode_responses=True)

    def get(self, key: str) -> Any | None:
        try:
            payload = self.client.get(f"{self.prefix}{key}")
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Redis cache unavailable, using memory cache: %s", exc)
            return None
        return json.loads(payload) if payload else None

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        try:
            self.client.set(
                f"{self.prefix}{key}",
                json.dumps(value, ensure_ascii=False),
                ex=ttl,
            )
        except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError):
            return


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
