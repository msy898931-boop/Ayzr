"""
The customer's invoice view.
============================

`/api/billing/history` is the only place a user sees their own money: what they
paid, when, and what the subscription is doing now. Two things must hold:

  1. it is scoped to the caller -- an order has an amount on it, so leaking one
     user's history to another would be a real breach;
  2. the numbers agree with the entitlement layer. If the page says "active"
     the quota system must agree, or support gets a confusing ticket.

The revenue view (`/api/admin/*`) is separate and fails closed; this endpoint is
for the customer, not the operator.
"""
from __future__ import annotations

import json
import time

import pytest

SECRET = "whsec_hist_123"
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


def buy(client, billing, plan="pro", auto_renew=True, ref="pi_h1", sub="sub_h1"):
    oid = client.post("/api/checkout",
                      json={"plan": plan, "auto_renew": auto_renew}).get_json()["order_id"]
    hook(client, billing, {"order_id": oid, "provider_ref": ref,
                           "provider_sub": sub, "status": "paid"})
    return oid


# ------------------------------------------------------------------- access
def test_history_requires_auth(client):
    r = client.get("/api/billing/history")
    assert r.status_code == 401


def test_history_is_empty_for_a_fresh_account(auth_client):
    d = auth_client.get("/api/billing/history").get_json()
    assert d["orders"] == []
    assert d["totals"]["count"] == 0
    assert d["subscription"]["active"] is False


def test_history_shows_a_paid_order(auth_client, billing):
    buy(auth_client, billing)
    d = auth_client.get("/api/billing/history").get_json()
    assert d["totals"]["count"] == 1
    assert d["totals"]["spent"] == pytest.approx(59.99)
    assert d["subscription"]["active"] is True
    assert d["subscription"]["auto_renew"] is True
    assert d["orders"][0]["plan"] == "pro"


def test_history_counts_only_paid_orders(auth_client, billing, clean):
    buy(auth_client, billing)
    uid = clean["uid"]
    db = clean["db"]
    db.create_order(uid, "pro", 59.99, "stripe")  # left pending on purpose
    d = auth_client.get("/api/billing/history").get_json()
    assert d["totals"]["count"] == 1          # the pending one is not revenue
    assert len(d["orders"]) == 2              # but it is still visible


def test_history_includes_a_renewal(auth_client, billing, clean):
    buy(auth_client, billing)
    hook(auth_client, billing, {"provider_sub": "sub_h1",
                                "provider_ref": "pi_renew1", "status": "paid"})
    d = auth_client.get("/api/billing/history").get_json()
    assert d["totals"]["count"] == 2
    assert d["totals"]["spent"] == pytest.approx(119.98)
    kinds = [o["kind"] for o in d["orders"]]
    assert "renewal" in kinds


def test_history_reports_days_left(auth_client, billing):
    buy(auth_client, billing)
    d = auth_client.get("/api/billing/history").get_json()
    assert 29 <= d["subscription"]["days_left"] <= 30
    assert d["subscription"]["plan_expires"] > time.time()


def test_history_reflects_a_cancel(auth_client, billing):
    buy(auth_client, billing)
    auth_client.post("/api/subscription/cancel")
    d = auth_client.get("/api/billing/history").get_json()
    assert d["subscription"]["auto_renew"] is False
    assert d["subscription"]["active"] is True   # access runs to the term


# ------------------------------------------------------------------ isolation
def test_history_never_leaks_another_users_orders(client, clean, billing):
    # user A buys...
    client.post("/api/auth/register",
                json={"email": "a@example.com", "password": "secret123"})
    buy(client, billing, ref="pi_a", sub="sub_a")
    assert client.get("/api/billing/history").get_json()["totals"]["count"] == 1
    client.post("/api/auth/logout")

    # ...user B must see none of it.
    client.post("/api/auth/register",
                json={"email": "b@example.com", "password": "secret123"})
    d = client.get("/api/billing/history").get_json()
    assert d["orders"] == []
    assert d["totals"]["count"] == 0


def test_history_agrees_with_entitlement(auth_client, billing, clean):
    buy(auth_client, billing)
    hist = auth_client.get("/api/billing/history").get_json()
    me = auth_client.get("/api/me").get_json()
    assert hist["subscription"]["active"] == me["user"]["active"]
    assert hist["subscription"]["plan"] == me["user"]["plan"]
    assert hist["subscription"]["days_left"] == me["user"]["days_left"]
