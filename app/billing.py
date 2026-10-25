"""
Billing.
========

Provider-agnostic payment plumbing, kept deliberately small and auditable:

    checkout_url(plan)      where to send a buyer (hosted checkout link)
    verify_signature(...)   constant-time HMAC check on an incoming webhook
    settle(...)             turn a verified payment into a plan, exactly once

The rule the whole module exists to enforce: **a plan is only ever granted by a
verified payment**. There is no code path where a user can name a plan and get
it for free -- the old self-service ``/api/plan`` switch is gone, and until a
provider is configured the endpoints refuse rather than silently upgrade.

Signature scheme (works for Stripe-style ``t=...,v1=...`` and a plain
``sha256=...`` header)::

    expected = hex(hmac_sha256(secret, f"{timestamp}.".encode() + raw_body))

The timestamp is optional; when present it is checked against a tolerance so a
captured webhook cannot be replayed a day later.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request

import config
import db

# How far a webhook timestamp may drift before we reject it as a replay.
REPLAY_TOLERANCE = 300


def checkout_url(plan: str) -> str | None:
    """A hosted-checkout URL for `plan`, or None when billing is unconfigured."""
    if not config.billing_configured() or not config.PAYMENT_LINK:
        return None
    return config.PAYMENT_LINK.replace("{plan}", plan)


def sign(payload: bytes, timestamp: int | None = None,
         secret: str | None = None) -> str:
    """The signature we expect for `payload`. Exposed so tests can forge one."""
    secret = secret if secret is not None else config.PAYMENT_WEBHOOK_SECRET
    prefix = f"{timestamp}.".encode() if timestamp is not None else b""
    return hmac.new(secret.encode(), prefix + payload, hashlib.sha256).hexdigest()


def parse_signature(header: str) -> tuple[int | None, str]:
    """Split a signature header into (timestamp, hex digest).

    Accepts both ``t=1700000000,v1=abc...`` and a bare ``abc...`` /
    ``sha256=abc...`` form.
    """
    header = (header or "").strip()
    if not header:
        return None, ""
    if "=" in header and "," in header:
        ts = None
        digest = ""
        for part in header.split(","):
            key, _, val = part.partition("=")
            key, val = key.strip(), val.strip()
            if key in ("t", "timestamp"):
                try:
                    ts = int(val)
                except ValueError:
                    ts = None
            elif key in ("v1", "sha256", "signature"):
                digest = val
        return ts, digest
    # bare form: "sha256=<hex>" or "<hex>"
    _, _, tail = header.partition("=")
    return None, (tail or header).strip()


def verify_signature(raw_body: bytes, header: str, secret: str | None = None,
                     now: float | None = None) -> bool:
    """True only if `header` is a valid HMAC of `raw_body` under the secret."""
    secret = secret if secret is not None else config.PAYMENT_WEBHOOK_SECRET
    if not secret:
        return False
    ts, digest = parse_signature(header)
    if not digest:
        return False
    if ts is not None:
        now = now if now is not None else time.time()
        if abs(now - ts) > REPLAY_TOLERANCE:
            return False
    expected = sign(raw_body, ts, secret)
    return hmac.compare_digest(expected, digest)


def settle(payload: dict) -> dict:
    """Grant the plan named by a *verified* webhook payload.

    Two shapes arrive here, both authenticated by the signature:

    * **First payment** -- ``{"order_id": 12, "provider_ref": "pi_123",
      "status": "paid", "provider_sub": "sub_9"}``. The order names the plan, so
      the caller cannot choose one; we record the subscription id for later.
    * **Renewal** -- ``{"provider_sub": "sub_9", "provider_ref": "in_456",
      "status": "paid"}`` with no ``order_id``. We look the subscription up in
      our own records to find the user and the plan, then extend the term. A
      renewal for a subscription we do not know is ignored, never granted.

    Idempotent either way: replaying the same ``provider_ref`` yields ``already``
    and never double-grants.
    """
    status = str(payload.get("status", "paid")).lower()
    if status not in ("paid", "succeeded", "complete", "completed"):
        return {"status": "ignored", "reason": f"status={status}"}

    provider = config.PAYMENT_PROVIDER or "manual"
    provider_ref = str(payload.get("provider_ref") or "").strip()
    if not provider_ref:
        return {"status": "ignored", "reason": "missing provider_ref"}

    order_id = payload.get("order_id")
    if order_id is None:
        return _settle_renewal(payload, provider, provider_ref)

    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        return {"status": "ignored", "reason": "bad order_id"}

    provider_sub = str(payload.get("provider_sub") or "").strip() or None
    result = db.activate_order(order_id, provider, provider_ref, provider_sub)
    return result


def _settle_renewal(payload: dict, provider: str, provider_ref: str) -> dict:
    """A recurring charge. Extend the term of the subscription it belongs to."""
    provider_sub = str(payload.get("provider_sub") or "").strip()
    if not provider_sub:
        return {"status": "ignored", "reason": "missing provider_sub"}
    user = db.user_for_sub(provider_sub)
    if user is None:
        # Unknown subscription: refuse rather than guess. A forged webhook that
        # passed the signature check still cannot conjure access.
        return {"status": "ignored", "reason": "unknown subscription"}
    # A provider retry of the same charge must not mint a second renewal order.
    prior = db.order_by_ref(provider, provider_ref)
    if prior is not None:
        if prior["status"] == "paid":
            return {"status": "already", "plan": prior["plan"],
                    "user_id": prior["user_id"]}
        return db.activate_order(prior["id"], provider, provider_ref, provider_sub)
    sub = db.active_subscription(user["id"])
    plan = sub["plan"] if sub else user["plan"]
    if plan not in db.PLANS or plan == "free":
        return {"status": "ignored", "reason": "no paid plan to renew"}
    amount = db.PLANS[plan]["price"]
    # A renewal is a real order, so revenue and history stay complete.
    order_id = db.create_order(user["id"], plan, amount, provider,
                               auto_renew=bool(sub and sub["auto_renew"]),
                               kind="renewal")
    result = db.activate_order(order_id, provider, provider_ref, provider_sub)
    if result.get("status") == "activated":
        result["renewal"] = True
    return result


def cancel_subscription(uid: int) -> dict:
    """Turn off automatic renewal. Access is *not* revoked -- it runs to term.

    Returns the provider subscription id so the route can best-effort tell the
    provider to stop billing; the local switch is what guarantees the customer
    is not charged again from our side.
    """
    sub = db.active_subscription(uid)
    if not sub:
        return {"status": "none"}
    db.set_auto_renew(uid, False, sub["provider_sub"])
    return {"status": "cancelled", "provider_sub": sub["provider_sub"],
            "until": db.days_left(db.get_user(uid))}


def provider_cancel(provider_sub: str) -> bool:
    """Best-effort: tell the provider to stop charging this subscription.

    Deliberately non-fatal. If the provider is unreachable the local
    `auto_renew=0` still holds, and the next renewal webhook would be honoured
    anyway (never take money and deny service) -- so a failed call here can only
    ever mean one extra charge that we refund, never a silently broken account.
    """
    url = config.PAYMENT_CANCEL_URL
    if not provider_sub or not url:
        return False
    # The URL is operator-configured, not caller-supplied, but pin the scheme so
    # a misconfiguration can never turn this into a file:/ or gopher: read.
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return False
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"subscription": provider_sub}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {config.PAYMENT_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 - scheme pinned above
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 - a provider failure must never break cancel
        return False
