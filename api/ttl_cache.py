"""
api/ttl_cache.py

A small in-memory cache for the public, read-only niche lists (/brand-niches, /brand-category-niches, /brand-category-tags,
/brand-niche-tags, /creator-sub-niches): their answers change only when the pipeline writes new niches, but each request ran a
DISTINCT query over a large table. Entries live NICHE_CACHE_SECONDS (default 300; 0 turns the cache off) and are keyed by the endpoint
and its query parameters; at most NICHE_CACHE_MAX_KEYS (default 200) keys are kept, the oldest dropped first, so a client that sends
many different parameters cannot grow it without limit. This cache holds public data only and is per process: a restart, or another
process, starts empty.
"""

import functools
import os
import threading
import time


def _int(name: str, default: int, minimum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def cache_seconds() -> int:
    return _int("NICHE_CACHE_SECONDS", 300, 0)


def cache_max_keys() -> int:
    return _int("NICHE_CACHE_MAX_KEYS", 200, 1)


cache_seconds(), cache_max_keys()   # a bad setting stops the application at start


class TTLCache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[object, tuple[float, object]] = {}

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def get_or_compute(self, key, compute):
        ttl = cache_seconds()
        if ttl == 0:
            return compute()
        now = self._clock()
        with self._lock:
            hit = self._items.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
        value = compute()   # outside the lock: a slow query must not block the other keys
        with self._lock:
            self._items.pop(key, None)
            while len(self._items) >= cache_max_keys():
                self._items.pop(next(iter(self._items)))   # the oldest inserted
            self._items[key] = (self._clock(), value)
        return value


niche_cache = TTLCache()


def cached_niche(name: str):
    """Decorator for a niche endpoint function: its result is cached per endpoint and query parameters."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (name, tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in kwargs.items())))
            return niche_cache.get_or_compute(key, lambda: fn(*args, **kwargs))
        return wrapper
    return decorator
