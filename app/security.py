"""
Abuse protection: per-IP rate limiting and login throttling.
============================================================

Two counters, one interface:

    allow(key)             spend one request from `key`'s budget
    record_failure(key)    count a failed login, locking `key` after too many
    record_success(key)    clear the failure streak
    locked(key)            seconds of lockout remaining, or 0

The default backend is in-process, which is enough for a single-worker
deployment but gives every worker its own counters -- so with ``gunicorn -w 4``
the effective limit is four times what you configured. When ``REDIS_URL`` is set
the counters move to Redis and the limit becomes global across workers and
hosts. That swap is the whole point of keeping the interface this narrow.

A Redis outage must never take the app down: the backend is chosen once at
import and, if Redis cannot be reached, we fall back to memory rather than
refuse every request.
"""
from __future__ import annotations

import threading
import time

import config


class _MemoryBackend:
    """Fixed-window counters in this process. The safe default."""

    name = "memory"

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lockouts: dict[str, float] = {}
        self._lock = threading.Lock()

    def count(self, key: str, window: int) -> int:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < window]
            if hits:
                self._hits[key] = hits
            else:
                self._hits.pop(key, None)
            return len(hits)

    def add(self, key: str, window: int) -> int:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < window]
            hits.append(now)
            self._hits[key] = hits
            return len(hits)

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def lock(self, key: str, seconds: int) -> None:
        with self._lock:
            self._lockouts[key] = time.time() + seconds

    def locked(self, key: str) -> int:
        with self._lock:
            until = self._lockouts.get(key, 0)
        return max(0, int(until - time.time()))

    def unlock(self, key: str) -> None:
        with self._lock:
            self._lockouts.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._lockouts.clear()


class _RedisBackend:
    """Counters shared across every worker, using Redis primitives.

    ``INCR`` + ``EXPIRE`` gives a fixed window without a Lua script; ``SET ... EX``
    gives a lockout with a TTL so it self-heals. Every method degrades to the
    in-memory backend on error so a Redis blip throttles nobody rather than
    blocking everybody.
    """

    name = "redis"

    def __init__(self, client) -> None:
        self._r = client
        self._fallback = _MemoryBackend()

    def _key(self, key: str) -> str:
        return f"tradli:{key}"

    def count(self, key: str, window: int) -> int:
        try:
            return int(self._r.get(self._key(key)) or 0)
        except Exception:  # noqa: BLE001
            return self._fallback.count(key, window)

    def add(self, key: str, window: int) -> int:
        try:
            k = self._key(key)
            n = int(self._r.incr(k))
            if n == 1:
                self._r.expire(k, window)
            return n
        except Exception:  # noqa: BLE001
            return self._fallback.add(key, window)

    def clear(self, key: str) -> None:
        try:
            self._r.delete(self._key(key))
        except Exception:  # noqa: BLE001
            self._fallback.clear(key)

    def lock(self, key: str, seconds: int) -> None:
        try:
            self._r.set(self._key("lock:" + key), "1", ex=seconds)
        except Exception:  # noqa: BLE001
            self._fallback.lock(key, seconds)

    def locked(self, key: str) -> int:
        try:
            ttl = self._r.ttl(self._key("lock:" + key))
            return ttl if isinstance(ttl, int) and ttl > 0 else 0
        except Exception:  # noqa: BLE001
            return self._fallback.locked(key)

    def unlock(self, key: str) -> None:
        try:
            self._r.delete(self._key("lock:" + key))
        except Exception:  # noqa: BLE001
            self._fallback.unlock(key)

    def reset(self) -> None:
        self._fallback.reset()


def _build_backend():
    url = getattr(config, "REDIS_URL", "")
    if not url:
        return _MemoryBackend()
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(url, socket_connect_timeout=2,
                                      socket_timeout=2, decode_responses=True)
        client.ping()
        return _RedisBackend(client)
    except Exception as exc:  # noqa: BLE001 - never fail closed on a Redis outage
        print(f"security: REDIS_URL set but Redis is unreachable ({exc}); "
              "falling back to in-process rate limiting", flush=True)
        return _MemoryBackend()


_backend = _build_backend()


def backend_name() -> str:
    return _backend.name


# ------------------------------------------------------------ rate limiting
def allow(key: str) -> bool:
    """True if `key` is within its request budget; records the hit if so."""
    if _backend.count(key, config.RATE_LIMIT_WINDOW) >= config.RATE_LIMIT_CALLS:
        return False
    _backend.add(key, config.RATE_LIMIT_WINDOW)
    return True


# --------------------------------------------------------- login throttling
def locked(key: str) -> int:
    """Seconds remaining on a lockout, or 0 if not locked."""
    return _backend.locked(key)


def record_failure(key: str) -> None:
    n = _backend.add(key, config.LOGIN_WINDOW)
    if n >= config.LOGIN_MAX_ATTEMPTS:
        _backend.lock(key, config.LOGIN_LOCKOUT)
        _backend.clear(key)


def record_success(key: str) -> None:
    _backend.clear(key)
    _backend.unlock(key)


def reset() -> None:
    """Test helper: forget all counters and lockouts."""
    _backend.reset()

