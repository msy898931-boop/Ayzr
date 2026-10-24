"""
Recurring billing: every renewal is a verified payment.
=========================================================

A subscription is only worth selling if renewals are handled correctly. The
provider charges the card automatically and tells us with an ``invoice.paid``
webhook; our job is to extend the term *exactly once per charge* and never to
grant access on the strength of a payload we cannot tie to a real subscription.

The rules these tests pin, in the order they would bite a real business:

  1. a renewal extends the term and adds a real order (so revenue is complete);
  2. replaying the same charge never extends twice;
  3. a renewal for an unknown subscription is refused, not granted;
  4. cancelling stops future charges but never revokes paid-for access;
  5. a charge that arrives after a cancel is still honoured -- we never take
     money and then deny the service.
"""
from __future__ import annotations

import json
import time

import pytest

SECRET = "whsec_rec_123"
DAY = 86400


@pytest.fixture()
def billing(clean, monkeypatch):
    b, cfg = clean["billing"], clean["config"]
    monkeypatch.setattr(cfg, "PAYMENT_PROVIDER", "stripe")
    monkeypatch.setattr(cfg, "PAYMENT_KEY", "sk_test")
    monkeypatch.setattr(cfg, "PAYMENT_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(cfg, "PAYMENT_LINK", "https://pay.example.com/{plan}")
    return b


def hook(client, billing, payload: dict):
    raw = json.dumps(payload).encode()
    ts = int(time.time())
    header = f"t={ts},v1={billing.sign(raw, ts, SECRET)}"
    return client.post("/api/webhook/stripe", data=raw,
                       headers={"X-Signature": header,
                                "Content-Type": "application/json"})


def _activate(client, billing, plan="pro", auto_renew=True, sub="sub_1", ref="pi_first"):
    order_id = client.post("/api/checkout",
                           json={"plan": plan, "auto_renew": auto_renew}).get_json()["order_id"]
    hook(client, billing, {"order_id": order_id, "provider_ref": ref,
                           "provider_sub": sub, "status": "paid"})
    return order_id


# ------------------------------------------------------------------ opt-in
def test_checkout_records_auto_renew(auth_client, billing, clean):
    order_id = auth_client.post("/api/checkout",
                                json={"plan": "pro", "auto_renew": True}).get_json()["order_id"]
    assert clean["db"].get_order(order_id)["auto_renew"] == 1


def test_checkout_defaults_to_no_auto_renew(auth_client, billing, clean):
    order_id = auth_client.post("/api/checkout", json={"plan": "pro"}).get_json()["order_id"]
    assert clean["db"].get_order(order_id)["auto_renew"] == 0


def test_me_reports_auto_renew(auth_client, billing, clean):
    _activate(auth_client, billing, auto_renew=True)
    assert auth_client.get("/api/me").get_json()["user"]["auto_renew"] is True


def test_subscription_id_is_recorded(auth_client, billing, clean):
    _activate(auth_client, billing, sub="sub_abc")
    sub = clean["db"].active_subscription(clean["uid"])
    assert sub["provider_sub"] == "sub_abc"


# ------------------------------------------------------------------ renewal
def test_renewal_extends_the_term(auth_client, billing, clean):
    _activate(auth_client, billing, plan="pro", sub="sub_x")
    before = auth_client.get("/api/me").get_json()["user"]["plan_expires"]
    r = hook(auth_client, billing, {"provider_sub": "sub_x", "provider_ref": "in_1",
                                    "status": "paid"})
    assert r.get_json()["status"] == "activated"
    assert r.get_json()["renewal"] is True
    after = auth_client.get("/api/me").get_json()["user"]["plan_expires"]
    assert abs(after - (before + 30 * DAY)) < 5


def test_renewal_creates_an_order(auth_client, billing, clean):
    _activate(auth_client, billing, plan="pro", sub="sub_y")
    hook(auth_client, billing, {"provider_sub": "sub_y", "provider_ref": "in_2",
                                "status": "paid"})
    kinds = [o["kind"] for o in clean["db"].list_orders(clean["uid"])]
    assert kinds.count("renewal") == 1


def test_renewal_is_idempotent(auth_client, billing, clean):
    _activate(auth_client, billing, plan="pro", sub="sub_z")
    first = hook(auth_client, billing, {"provider_sub": "sub_z", "provider_ref": "in_dup",
                                        "status": "paid"})
    expiry_once = auth_client.get("/api/me").get_json()["user"]["plan_expires"]
    second = hook(auth_client, billing, {"provider_sub": "sub_z", "provider_ref": "in_dup",
                                         "status": "paid"})
    assert first.get_json()["status"] == "activated"
    assert second.get_json()["status"] == "already"
    # The second delivery must not have moved the expiry a second time.
    assert auth_client.get("/api/me").get_json()["user"]["plan_expires"] == expiry_once


def test_renewal_for_unknown_subscription_is_refused(auth_client, billing, clean):
    r = hook(auth_client, billing, {"provider_sub": "sub_nope", "provider_ref": "in_x",
                                    "status": "paid"})
    assert r.get_json()["status"] == "ignored"
    assert auth_client.get("/api/me").get_json()["user"]["plan"] == "free"


def test_renewal_without_sub_id_is_ignored(auth_client, billing, clean):
    r = hook(auth_client, billing, {"provider_ref": "in_x", "status": "paid"})
    assert r.get_json()["status"] == "ignored"


def test_renewal_after_lapse_restarts_from_now(auth_client, billing, clean):
    _activate(auth_client, billing, plan="pro", sub="sub_lapse")
    # Pretend the term lapsed long ago.
    clean["db"].set_plan(clean["uid"], "pro", days=30, now=time.time() - 40 * DAY)
    r = hook(auth_client, billing, {"provider_sub": "sub_lapse", "provider_ref": "in_late",
                                    "status": "paid"})
    assert r.get_json()["status"] == "activated"
    exp = auth_client.get("/api/me").get_json()["user"]["plan_expires"]
    assert abs(exp - (time.time() + 30 * DAY)) < 5


def test_renewal_plan_comes_from_our_records_not_the_payload(auth_client, billing, clean):
    _activate(auth_client, billing, plan="basic", sub="sub_plan")
    # A forged-but-signed payload cannot upgrade the plan it is renewing.
    hook(auth_client, billing, {"provider_sub": "sub_plan", "provider_ref": "in_up",
                                "status": "paid", "plan": "ultimate"})
    assert auth_client.get("/api/me").get_json()["user"]["plan"] == "basic"


def test_renewal_of_expired_plan_uses_the_subscribed_plan(auth_client, billing, clean):
    # The stored plan stays 'pro' after expiry, so the renewal knows what to grant.
    _activate(auth_client, billing, plan="pro", sub="sub_keep")
    clean["db"].set_plan(clean["uid"], "pro", days=30, now=time.time() - 40 * DAY)
    hook(auth_client, billing, {"provider_sub": "sub_keep", "provider_ref": "in_keep",
                                "status": "paid"})
    u = auth_client.get("/api/me").get_json()["user"]
    assert u["plan"] == "pro" and u["active"] is True


# ------------------------------------------------------------------- cancel
def test_cancel_requires_auth(client):
    assert client.post("/api/subscription/cancel").status_code == 401


def test_cancel_without_subscription_is_404(auth_client):
    assert auth_client.post("/api/subscription/cancel").status_code == 404


def test_cancel_turns_off_auto_renew(auth_client, billing, clean):
    _activate(auth_client, billing, auto_renew=True, sub="sub_c")
    r = auth_client.post("/api/subscription/cancel")
    assert r.status_code == 200
    assert r.get_json()["status"] == "cancelled"
    assert auth_client.get("/api/me").get_json()["user"]["auto_renew"] is False


def test_cancel_keeps_access_until_term_end(auth_client, billing, clean):
    _activate(auth_client, billing, plan="pro", auto_renew=True, sub="sub_d")
    before = auth_client.get("/api/me").get_json()["user"]["days_left"]
    auth_client.post("/api/subscription/cancel")
    u = auth_client.get("/api/me").get_json()["user"]
    assert u["active"] is True           # paid-for time is never revoked early
    assert u["days_left"] == before
    assert u["plan"] == "pro"


def test_charge_after_cancel_is_still_honoured(auth_client, billing, clean):
    # The race the customer must always win: they were charged, so they get time.
    _activate(auth_client, billing, plan="pro", auto_renew=True, sub="sub_e")
    before = auth_client.get("/api/me").get_json()["user"]["plan_expires"]
    auth_client.post("/api/subscription/cancel")
    r = hook(auth_client, billing, {"provider_sub": "sub_e", "provider_ref": "in_race",
                                    "status": "paid"})
    assert r.get_json()["status"] == "activated"
    after = auth_client.get("/api/me").get_json()["user"]["plan_expires"]
    assert after > before  # the money bought time, as it must


# ----------------------------------------------------------- provider cancel
def test_provider_cancel_is_a_noop_when_unconfigured(billing, monkeypatch):
    monkeypatch.setattr(billing.config, "PAYMENT_CANCEL_URL", "")
    assert billing.provider_cancel("sub_1") is False


def test_provider_cancel_never_raises_on_a_bad_endpoint(billing, monkeypatch):
    # An unreachable provider must not break the local cancel path.
    monkeypatch.setattr(billing.config, "PAYMENT_CANCEL_URL", "http://127.0.0.1:1/cancel")
    assert billing.provider_cancel("sub_1") is False


# --------------------------------------------------------------- reporting
def test_renewal_revenue_reaches_the_admin_stats(client, clean, billing, monkeypatch):
    monkeypatch.setattr(clean["config"], "ADMIN_TOKEN", "tok")
    uid = clean["db"].create_user("rev@x.com", "secret123")["id"]
    clean["db"].activate_order(
        clean["db"].create_order(uid, "pro", 59.99, "stripe"),
        "stripe", "pi_r", "sub_rev")
    clean["billing"].settle({"provider_sub": "sub_rev", "provider_ref": "in_r",
                             "status": "paid"})
    body = client.get("/api/admin/stats",
                      headers={"Authorization": "Bearer tok"}).get_json()
    assert body["orders"]["paid"] == 2
    assert body["orders"]["revenue"] == pytest.approx(119.98, abs=0.01)
