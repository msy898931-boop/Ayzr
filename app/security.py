"""
Security layer: abuse protection, CSRF, headers and safe logging.
================================================================

One module owns every request-level protection, so there is a single place to
read and a single place to change. It is split in two halves:

Abuse protection (framework-agnostic counters)
    allow(key)             spend one request from `key`'s budget
    record_failure(key)    count a failed login, locking `key` after too many
    record_success(key)    clear the failure streak
    locked(key)            seconds of lockout remaining, or 0

Web hardening (wired by `init_app(app)`)
    a fresh CSP nonce per request, and a strict Content-Security-Policy
    a per-session CSRF token, required on every state-changing request
    a Content-Type guard that rejects "simple" cross-site POSTs
    a fixed set of response headers (nosniff, frame-deny, HSTS in prod, ...)
    `log_event`/`redact` so a secret never reaches the log

The default rate-limit backend is in-process, which is enough for a single
worker but gives every worker its own counters -- so with ``gunicorn -w 4`` the
effective limit is four times what you configured. When ``REDIS_URL`` is set the
counters move to Redis and the limit becomes global across workers and hosts.

A Redis outage must never take the app down: the backend is chosen once at
import and, if Redis cannot be reached, we fall back to memory rather than
refuse every request.
"""
from __future__ import annotations

import logging
import re
import secrets
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


# ==========================================================================
# Safe logging
# ==========================================================================
# A secret must never reach a log line, a crash report or a support paste. We
# mask the shapes that actually leak in practice: bearer tokens, ``key=value``
# pairs, and the prefixes used by the payment and model providers. This is a
# safety net, not a licence to log secrets -- callers should still pass only
# what they need.
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:api[_-]?key|secret|token|password|passwd|pwd|authorization"
               r"|signature|v1)\"?\s*[:=]\s*\"?)([^\s\"',&}\]]+)"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]+"),
    re.compile(r"\bwhsec_[A-Za-z0-9]+"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{10,}"),
    re.compile(r"\bgsk_[A-Za-z0-9]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{12,}"),
)

_REDACTED = "***"


def redact(text) -> str:
    """Return `text` with anything that looks like a secret masked."""
    out = str(text)
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) + _REDACTED) if m.groups() else _REDACTED,
                      out)
    return out


def log_event(logger, level: str, event: str, **fields) -> None:
    """Log a structured event with every value passed through `redact`."""
    parts = [f"{k}={redact(v)}" for k, v in fields.items() if v is not None]
    line = event + (" " + " ".join(parts) if parts else "")
    logger.log(getattr(logging, level.upper()), redact(line))


# ==========================================================================
# Input guards (framework-agnostic where possible)
# ==========================================================================
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def safe_next(target, default: str = "/") -> str:
    """Constrain a user-supplied redirect to a same-origin *path*.

    Rejects anything that is not a plain absolute path: scheme-relative URLs
    (``//evil``), absolute URLs (``https://evil``), and control characters or
    backslashes that browsers may normalise into a host switch. This is the only
    function a route should use to honour a ``?next=`` parameter.
    """
    if not target or not isinstance(target, str):
        return default
    if not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return default
    if "\\" in target or any(c in target for c in "\r\n\t\x00"):
        return default
    return target


def looks_like_json_route(path: str) -> bool:
    """True for API paths whose body must be JSON."""
    return path.startswith("/api/")


# ==========================================================================
# CSRF + headers (require Flask; imported lazily so this module stays usable
# on its own, e.g. from the abuse-protection tests)
# ==========================================================================
CSRF_HEADER = "X-CSRF-Token"
CSRF_SESSION_KEY = "_csrf"

# Machine-to-machine endpoints authenticate with an HMAC signature or a bearer
# token, not a session cookie, so a CSRF token would be meaningless there.
CSRF_EXEMPT_PREFIXES = ("/api/webhook/", "/api/signals/ingest")


def _exempt(path: str) -> bool:
    extra = tuple(getattr(config, "CSRF_EXEMPT_PREFIXES", ()) or ())
    return any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES + extra)


def csrf_token() -> str:
    """The current session's CSRF token, minting one on first use."""
    from flask import session

    tok = session.get(CSRF_SESSION_KEY)
    if not tok:
        tok = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = tok
    return tok


def csrf_ok() -> bool:
    """True when the request may proceed under CSRF rules.

    A state-changing request must carry a token that matches the one bound to
    the caller's session; safe methods and exempt paths always pass. The
    comparison is constant-time so the token cannot be guessed byte by byte.
    """
    from flask import request, session

    if request.method in _SAFE_METHODS:
        return True
    if _exempt(request.path):
        return True
    if not getattr(config, "CSRF_PROTECT", True):
        return True
    sent = request.headers.get(CSRF_HEADER) or request.form.get("_csrf", "")
    stored = session.get(CSRF_SESSION_KEY, "")
    return bool(stored) and bool(sent) and secrets.compare_digest(sent, stored)


def json_content_type_ok() -> bool:
    """Reject a body-carrying state change that is not ``application/json``.

    This closes the "simple request" hole: a cross-site ``<form>`` or
    ``fetch(..., {mode:'no-cors'})`` can POST ``text/plain`` without a CORS
    preflight, so the Content-Type itself is the signal we refuse.
    """
    from flask import request

    if request.method in _SAFE_METHODS:
        return True
    if _exempt(request.path):
        return True
    if not getattr(config, "REQUIRE_JSON_CONTENT_TYPE", True):
        return True
    if not request.get_data(cache=True):
        return True  # empty body: nothing to smuggle
    return request.mimetype == "application/json"


def content_security_policy(nonce: str, *, connect_extra=()) -> str:
    """A strict CSP built around a per-request nonce.

    ``script-src`` never contains ``unsafe-inline``/``unsafe-eval``: the page's
    own inline script is allowed by nonce, so an injected ``<script>`` has no
    nonce and is refused. ``connect-src`` lists only same-origin plus whatever a
    caller explicitly adds.
    """
    connect = " ".join(("'self'", *connect_extra))
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data: blob:; "
        f"connect-src {connect}; "
        "font-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    )


def apply_security_headers(resp, nonce: str = "", *, is_prod=None):
    """Set the fixed response headers on `resp` and return it."""
    is_prod = config.IS_PROD if is_prod is None else is_prod
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    resp.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    if nonce:
        resp.headers.setdefault("Content-Security-Policy",
                                content_security_policy(nonce))
    if is_prod and getattr(config, "HSTS_SECONDS", 0):
        resp.headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={config.HSTS_SECONDS}; includeSubDomains")
    return resp


def init_app(app) -> None:
    """Wire the request-level protections into a Flask app.

    Installs one ``before_request`` that mints the per-request CSP nonce and the
    per-session CSRF token, and one ``after_request`` that stamps the security
    headers. Called once from ``server.py``; keeping it here means the app file
    does not carry the policy text.
    """
    from flask import g

    @app.before_request
    def _security_context():  # noqa: ANN202 - Flask view
        g.csp_nonce = secrets.token_urlsafe(16)
        g.csrf_token = csrf_token()

    @app.after_request
    def _security_headers(resp):  # noqa: ANN001, ANN202 - Flask response
        return apply_security_headers(resp, getattr(g, "csp_nonce", ""))


