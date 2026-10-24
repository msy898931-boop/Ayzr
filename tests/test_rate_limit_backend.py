"""
Rate-limit backend selection.
=============================

The interface (allow / record_failure / locked) must behave the same whether the
counters live in this process or in Redis. These tests pin the memory backend
and the Redis *fallback* -- a Redis URL that cannot be reached must degrade to
memory rather than refuse every request.
"""
from __future__ import annotations


def test_default_backend_is_memory(mods):
    assert mods["security"].backend_name() == "memory"


def test_memory_backend_enforces_budget(mods, monkeypatch):
    sec = mods["security"]
    cfg = mods["config"]
    monkeypatch.setattr(cfg, "RATE_LIMIT_CALLS", 3)
    monkeypatch.setattr(cfg, "RATE_LIMIT_WINDOW", 60)
    sec.reset()
    assert [sec.allow("ip:1.2.3.4") for _ in range(4)] == [True, True, True, False]
    # A different key has its own budget.
    assert sec.allow("ip:5.6.7.8") is True


def test_login_lockout_after_max_attempts(mods, monkeypatch):
    sec = mods["security"]
    cfg = mods["config"]
    monkeypatch.setattr(cfg, "LOGIN_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(cfg, "LOGIN_WINDOW", 300)
    monkeypatch.setattr(cfg, "LOGIN_LOCKOUT", 900)
    sec.reset()
    for _ in range(3):
        sec.record_failure("login:9.9.9.9")
    assert sec.locked("login:9.9.9.9") > 0
    sec.record_success("login:9.9.9.9")
    assert sec.locked("login:9.9.9.9") == 0


def test_redis_url_unreachable_falls_back_to_memory(mods, monkeypatch):
    """A bad REDIS_URL must not take the app down or fail closed."""
    sec = mods["security"]
    monkeypatch.setattr(mods["config"], "REDIS_URL", "redis://127.0.0.1:1/0")
    backend = sec._build_backend()
    assert backend.name == "memory"
    # And it still enforces.
    assert backend.add("k", 60) == 1


class _FakeRedis:
    """A tiny stand-in for redis-py, enough to exercise _RedisBackend."""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.expiry: dict[str, int] = {}

    def get(self, k):
        return self.store.get(k)

    def incr(self, k):
        self.store[k] = int(self.store.get(k, 0)) + 1
        return self.store[k]

    def expire(self, k, s):
        self.expiry[k] = s
        return True

    def delete(self, k):
        self.store.pop(k, None)
        self.expiry.pop(k, None)
        return 1

    def set(self, k, v, ex=None):
        self.store[k] = v
        if ex:
            self.expiry[k] = ex
        return True

    def ttl(self, k):
        return self.expiry.get(k, -2)


def test_redis_backend_shares_counters(mods):
    sec = mods["security"]
    backend = sec._RedisBackend(_FakeRedis())
    assert backend.name == "redis"
    assert [backend.add("ip:1", 60) for _ in range(3)] == [1, 2, 3]
    backend.lock("ip:1", 900)
    assert backend.locked("ip:1") == 900
    backend.unlock("ip:1")
    assert backend.locked("ip:1") == 0


def test_redis_backend_degrades_on_error(mods):
    """If a Redis call raises, the backend must keep working, not crash."""
    class _Broken:
        def __getattr__(self, _n):
            raise RuntimeError("redis down")

    backend = mods["security"]._RedisBackend(_Broken())
    assert backend.add("k", 60) == 1        # served by the memory fallback
    assert backend.count("k", 60) == 1
    backend.lock("k", 900)
    assert backend.locked("k") > 0
