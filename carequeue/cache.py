"""Short-lived cache for a provider's day schedule (Redis, or in memory without REDIS_URL).

Schedules are read far more often than they change, so each (provider, day) is cached
for a short TTL and explicitly invalidated whenever a booking, cancellation, hold or
expiry touches that day. The TTL is only a safety net; invalidation keeps reads fresh.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date


def _key(provider_id: int, day: date) -> str:
    return f"cq:schedule:{provider_id}:{day.isoformat()}"


class MemoryScheduleCache:
    backend = "memory"

    def __init__(self, ttl_s: int = 60):
        self.ttl_s = ttl_s
        self._data: dict[str, tuple[float, list]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, provider_id: int, day: date) -> list | None:
        with self._lock:
            item = self._data.get(_key(provider_id, day))
            if item and item[0] > time.monotonic():
                self.hits += 1
                return item[1]
            self.misses += 1
            return None

    def set(self, provider_id: int, day: date, value: list) -> None:
        with self._lock:
            self._data[_key(provider_id, day)] = (time.monotonic() + self.ttl_s, value)

    def invalidate(self, provider_id: int, day: date) -> None:
        with self._lock:
            self._data.pop(_key(provider_id, day), None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class RedisScheduleCache:
    backend = "redis"

    def __init__(self, url: str, ttl_s: int = 60):
        import redis

        self.ttl_s = ttl_s
        self._r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
        self.hits = 0
        self.misses = 0

    def get(self, provider_id: int, day: date) -> list | None:
        try:
            raw = self._r.get(_key(provider_id, day))
        except Exception:  # noqa: BLE001 - a cache outage must never break reads
            return None
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(raw)

    def set(self, provider_id: int, day: date, value: list) -> None:
        try:
            self._r.set(_key(provider_id, day), json.dumps(value), ex=self.ttl_s)
        except Exception:  # noqa: BLE001
            pass

    def invalidate(self, provider_id: int, day: date) -> None:
        try:
            self._r.delete(_key(provider_id, day))
        except Exception:  # noqa: BLE001
            pass

    def clear(self) -> None:
        try:
            keys = list(self._r.scan_iter("cq:schedule:*"))
            if keys:
                self._r.delete(*keys)
        except Exception:  # noqa: BLE001
            pass


def make_cache(redis_url: str | None, ttl_s: int):
    return RedisScheduleCache(redis_url, ttl_s) if redis_url else MemoryScheduleCache(ttl_s)
