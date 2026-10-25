"""
Central configuration.
======================

Everything the app reads from the environment lives here, in one place, so the
defaults are visible and testable. `db.py` and `llm.py` read their own paths
today; new code should prefer `config`.

Security notes:
    SECRET_KEY   A random key per boot invalidates every session on restart and
                 breaks multi-worker deployments (each worker gets its own key).
                 Set SECRET_KEY in production; we only fall back to urandom in
                 development so a missing key never silently ships.
    TRUST_PROXY  Honour X-Forwarded-For only when behind a proxy you control,
                 otherwise a client can spoof its IP and defeat rate limiting.
"""
from __future__ import annotations

import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VERSION = "1.9.0"

# The product name shown in the UI, page title and footer. Kept in config (not
# hard-coded) so an operator can white-label without touching the template, and
# so the shipped default carries no third-party trademark.
BRAND_NAME = os.environ.get("BRAND_NAME", "Aurora Markets").strip() or "Aurora Markets"

# Contact address shown on the legal pages. Set this to a real mailbox before
# going live; the placeholder is deliberately obvious.
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "support@example.com").strip()


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ENV = os.environ.get("TRADLI_ENV", "development").strip().lower()
IS_PROD = ENV in {"prod", "production"}

PORT = _int("PORT", 8000)

# Login throttling: after MAX_ATTEMPTS failures within WINDOW seconds an IP is
# locked out for LOCKOUT seconds.
LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 8)
LOGIN_WINDOW = _int("LOGIN_WINDOW", 300)
LOGIN_LOCKOUT = _int("LOGIN_LOCKOUT", 900)

# Per-IP request budget for the expensive endpoints (calls that hit the model
# or a market data provider). Generous enough for a human, tight enough that a
# script cannot burn the quota of every provider in a minute.
RATE_LIMIT_CALLS = _int("RATE_LIMIT_CALLS", 60)
RATE_LIMIT_WINDOW = _int("RATE_LIMIT_WINDOW", 60)

TRUST_PROXY = _bool("TRUST_PROXY", False)

# Optional Redis for rate limiting. Unset (the default) keeps the counters in
# process, which is correct for a single worker. Set it to share one limit
# across every gunicorn worker and host: redis://host:6379/0
REDIS_URL = os.environ.get("REDIS_URL", "").strip()

# ------------------------------------------------------------------- security
# CSRF protection for state-changing requests. Enabled by default; only turn it
# off if a trusted non-browser client is posting to the API (it weakens the
# cookie-based protections). A token is issued per session and required in the
# X-CSRF-Token header on every POST.
CSRF_PROTECT = _bool("CSRF_PROTECT", True)

# Reject a JSON API call whose Content-Type is not application/json. This blocks
# "simple" cross-site POSTs (text/plain, form-urlencoded) that skip the CORS
# preflight, so a browser cannot be made to fire an authenticated mutation.
REQUIRE_JSON_CONTENT_TYPE = _bool("REQUIRE_JSON_CONTENT_TYPE", True)

# Password policy. The minimum is deliberately short enough for the product's
# free tier but rejects the passwords that show up in every credential-stuffing
# list. Set PASSWORD_MIN_LENGTH=1 to disable (not recommended).
PASSWORD_MIN_LENGTH = _int("PASSWORD_MIN_LENGTH", 8)

# A top-level domain that should never appear in a real signup. "example.com" is
# reserved (RFC 2606), so blocking it turns the shipped sample credentials into
# an obvious, harmless mistake rather than a live account.
BLOCKED_EMAIL_DOMAINS = tuple(
    d.strip().lower() for d in
    os.environ.get("BLOCKED_EMAIL_DOMAINS", "example.com,example.org,example.net,test,invalid,localhost").split(",")
    if d.strip())

# HSTS: only send when the app is served over HTTPS (production behind TLS).
HSTS_SECONDS = _int("HSTS_SECONDS", 31536000)

# Where a user may be sent after login. Only same-origin relative paths are
# honoured; an absolute or scheme-relative URL is dropped. Kept here so the
# allow-list is one obvious place.
SAFE_REDIRECT_PREFIXES = ("/",)

# Log level for the request logger (DEBUG/INFO/WARNING/ERROR).
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

# ---------------------------------------------------------------- billing
# Payments are provider-agnostic. A provider is configured by giving it both a
# key and a webhook secret; until then the app stays in "demo billing" mode and
# self-upgrades are refused, so no plan can be granted without a real payment.
PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "").strip().lower()
PAYMENT_KEY = os.environ.get("PAYMENT_KEY", "").strip()
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "").strip()

# A checkout link template. {plan} is substituted, so an operator can point each
# plan at its own hosted checkout without changing code.
PAYMENT_LINK = os.environ.get("PAYMENT_LINK", "").strip()

# Where the provider sends the buyer back after paying (our own pages).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")

# Optional provider endpoint that cancels a subscription when the customer turns
# auto-renewal off. Unset means cancel is local-only, which is safe: the switch
# is what stops us honouring the next charge from our side.
PAYMENT_CANCEL_URL = os.environ.get("PAYMENT_CANCEL_URL", "").strip()

# Live spot prices for metals. Yahoo quotes the *futures* contract (GC=F), which
# trades a few dollars away from the spot price a trader sees on MT5; this feed
# supplies the real spot so the numbers match the broker. Set to "" to disable.
GOLD_API_URL = os.environ.get("GOLD_API_URL",
                              "https://api.gold-api.com/price/{sym}").strip()
GOLD_API_TTL = float(os.environ.get("GOLD_API_TTL", "60") or 60)


def billing_configured() -> bool:
    """True only when a real provider could take money right now."""
    return bool(PAYMENT_PROVIDER and PAYMENT_KEY)


def webhook_configured() -> bool:
    """True only when incoming webhooks can be authenticated."""
    return bool(PAYMENT_WEBHOOK_SECRET)


# A bearer token for the read-only admin endpoints. Unset means the admin API
# is off entirely (it fails closed with 503, never open).
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()


def admin_configured() -> bool:
    return bool(ADMIN_TOKEN)


# A bearer token for the signal ingest endpoint. Unset means ingest is off
# (503): a public "add a signal" endpoint would be a spam and XSS vector.
SIGNAL_INGEST_TOKEN = os.environ.get("SIGNAL_INGEST_TOKEN", "").strip()


def ingest_configured() -> bool:
    return bool(SIGNAL_INGEST_TOKEN)



def secret_key() -> str:
    """A stable session key, generated once in development if not provided."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    if IS_PROD:
        print("FATAL: SECRET_KEY must be set in production "
              "(sessions would reset on every restart).", file=sys.stderr)
        raise SystemExit(2)
    # Development: derive a key that is stable for the life of the process but
    # not predictable across deployments.
    return os.environ.setdefault("_TRADLI_DEV_KEY", secrets.token_hex(32))


def security_status() -> dict:
    """A single, honest summary of what protection is actually on right now.

    Surfaced by ``/api/health`` so an operator (or a monitoring probe) can see,
    at a glance, which layers are enabled and which are still on their insecure
    development default. Every value here is a *setting*, not a claim: nothing
    is reported as on unless the corresponding code path is active.
    """
    from security import backend_name  # imported lazily: security imports config
    return {
        "csrf": CSRF_PROTECT,
        "require_json_content_type": REQUIRE_JSON_CONTENT_TYPE,
        "password_min_length": PASSWORD_MIN_LENGTH,
        "login_lockout": {"attempts": LOGIN_MAX_ATTEMPTS, "window_s": LOGIN_WINDOW,
                          "lockout_s": LOGIN_LOCKOUT},
        "rate_limit": {"calls": RATE_LIMIT_CALLS, "window_s": RATE_LIMIT_WINDOW,
                       "backend": backend_name()},
        "hsts": bool(IS_PROD and HSTS_SECONDS),
        "session_cookie_secure": bool(IS_PROD),
        "secret_key_set": bool(os.environ.get("SECRET_KEY")),
        "trust_proxy": TRUST_PROXY,
        "admin_api": admin_configured(),
        "signal_ingest": ingest_configured(),
        "billing": billing_configured(),
        "webhook": webhook_configured(),
    }

