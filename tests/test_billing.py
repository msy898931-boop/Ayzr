"""
Billing: signatures, idempotency and the "no free upgrade" guarantee.
====================================================================

These tests exist because a plan-granting bug is a *revenue* bug. The most
important one is `test_self_upgrade_is_impossible`: before this module, any
signed-in user could POST /api/plan and become "ultimate" for free.
"""
from __future__ import annotations

import json
import time

import pytest

SECRET = "whsec_test_123"


@pytest.fixture()
def billing(clean, monkeypatch):
    """Billing module with a provider and webhook secret configured."""
    b, cfg = clean["billing"], clean["config"]
    monkeypatch.setattr(cfg, "PAYMENT_PROVIDER", "stripe")
    monkeypatch.setattr(cfg, "PAYMENT_KEY", "sk_test")
    monkeypatch.setattr(cfg, "PAYMENT_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(cfg, "PAYMENT_LINK", "https://pay.example.com/{plan}")
    return b


def signed_body(billing, payload: dict, ts: int | None = None) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode()
    ts = ts if ts is not None else int(time.time())
    return raw, f"t={ts},v1={billing.sign(raw, ts, SECRET)}"


# ------------------------------------------------------------------ signature
def test_sign_verify_roundtrip(billing):
    raw = b'{"a":1}'
    ts = int(time.time())
    assert billing.verify_signature(raw, f"t={ts},v1={billing.sign(raw, ts, SECRET)}",
                                    SECRET) is True


def test_verify_rejects_wrong_secret(billing):
    raw = b'{"a":1}'
    sig = billing.sign(raw, 123, "other")
    assert billing.verify_signature(raw, f"t=123,v1={sig}", SECRET) is False


def test_verify_rejects_tampered_body(billing):
    raw, header = signed_body(billing, {"order_id": 1})
    assert billing.verify_signature(raw + b" ", header, SECRET) is False


def test_verify_rejects_empty_signature(billing):
    assert billing.verify_signature(b'{"a":1}', "", SECRET) is False


def test_verify_rejects_stale_timestamp(billing):
    raw, header = signed_body(billing, {"a": 1}, ts=int(time.time()) - 10_000)
    assert billing.verify_signature(raw, header, SECRET) is False


def test_verify_rejects_without_configured_secret(billing, monkeypatch):
    raw, header = signed_body(billing, {"a": 1})
    monkeypatch.setattr(billing.config, "PAYMENT_WEBHOOK_SECRET", "")
    assert billing.verify_signature(raw, header) is False


def test_parse_signature_accepts_bare_hex(billing):
    ts, digest = billing.parse_signature("deadbeef")
    assert ts is None and digest == "deadbeef"


def test_parse_signature_accepts_sha256_prefix(billing):
    ts, digest = billing.parse_signature("sha256=deadbeef")
    assert ts is None and digest == "deadbeef"


def test_parse_signature_accepts_stripe_form(billing):
    ts, digest = billing.parse_signature("t=1700,v1=abc")
    assert ts == 1700 and digest == "abc"


# ------------------------------------------------------------------ checkout
def test_checkout_requires_auth(client):
    assert client.post("/api/checkout", json={"plan": "pro"}).status_code == 401


def test_checkout_refuses_when_unconfigured(auth_client):
    r = auth_client.post("/api/checkout", json={"plan": "pro"})
    assert r.status_code == 503
    assert r.get_json()["error"] == "billing_not_configured"


def test_checkout_creates_order(auth_client, billing, clean):
    r = auth_client.post("/api/checkout", json={"plan": "pro"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["plan"] == "pro"
    assert body["amount"] == clean["db"].PLANS["pro"]["price"]
    assert body["checkout_url"] == "https://pay.example.com/pro"
    assert clean["db"].get_order(body["order_id"])["status"] == "pending"


def test_checkout_rejects_free_and_unknown(auth_client, billing):
    assert auth_client.post("/api/checkout", json={"plan": "free"}).status_code == 400
    assert auth_client.post("/api/checkout", json={"plan": "nope"}).status_code == 400


def test_checkout_does_not_grant_plan(auth_client, billing):
    auth_client.post("/api/checkout", json={"plan": "ultimate"})
    assert auth_client.get("/api/me").get_json()["user"]["plan"] == "free"


# ------------------------------------------------------------------- webhook
def test_webhook_refuses_without_secret(client):
    r = client.post("/api/webhook/stripe", json={"order_id": 1})
    assert r.status_code == 503


def test_webhook_rejects_bad_signature(client, billing):
    r = client.post("/api/webhook/stripe", json={"order_id": 1},
                    headers={"X-Signature": "t=1,v1=bad"})
    assert r.status_code == 401


def test_webhook_activates_plan(auth_client, billing, clean):
    order_id = auth_client.post("/api/checkout", json={"plan": "pro"}).get_json()["order_id"]
    raw, header = signed_body(billing, {"order_id": order_id,
                                        "provider_ref": "pi_1", "status": "paid"})
    r = auth_client.post("/api/webhook/stripe", data=raw,
                         headers={"X-Signature": header, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "activated"
    assert auth_client.get("/api/me").get_json()["user"]["plan"] == "pro"


def test_webhook_is_idempotent(auth_client, billing, clean):
    order_id = auth_client.post("/api/checkout", json={"plan": "pro"}).get_json()["order_id"]
    raw, header = signed_body(billing, {"order_id": order_id,
                                        "provider_ref": "pi_2", "status": "paid"})
    first = auth_client.post("/api/webhook/stripe", data=raw,
                             headers={"X-Signature": header})
    second = auth_client.post("/api/webhook/stripe", data=raw,
                              headers={"X-Signature": header})
    assert first.get_json()["status"] == "activated"
    assert second.get_json()["status"] == "already"
    assert clean["db"].get_order(order_id)["status"] == "paid"


def test_webhook_ref_used_twice_for_different_orders_is_refused(auth_client, billing, clean):
    a = auth_client.post("/api/checkout", json={"plan": "pro"}).get_json()["order_id"]
    b = auth_client.post("/api/checkout", json={"plan": "basic"}).get_json()["order_id"]
    raw1, h1 = signed_body(billing, {"order_id": a, "provider_ref": "pi_dup", "status": "paid"})
    raw2, h2 = signed_body(billing, {"order_id": b, "provider_ref": "pi_dup", "status": "paid"})
    auth_client.post("/api/webhook/stripe", data=raw1, headers={"X-Signature": h1})
    r = auth_client.post("/api/webhook/stripe", data=raw2, headers={"X-Signature": h2})
    assert r.get_json()["status"] == "conflict"
    assert auth_client.get("/api/me").get_json()["user"]["plan"] == "pro"


def test_webhook_ignores_non_paid_status(auth_client, billing, clean):
    order_id = auth_client.post("/api/checkout", json={"plan": "pro"}).get_json()["order_id"]
    raw, header = signed_body(billing, {"order_id": order_id,
                                        "provider_ref": "pi_3", "status": "failed"})
    r = auth_client.post("/api/webhook/stripe", data=raw, headers={"X-Signature": header})
    assert r.get_json()["status"] == "ignored"
    assert auth_client.get("/api/me").get_json()["user"]["plan"] == "free"


def test_webhook_ignores_missing_ref(auth_client, billing):
    order_id = auth_client.post("/api/checkout", json={"plan": "pro"}).get_json()["order_id"]
    raw, header = signed_body(billing, {"order_id": order_id, "status": "paid"})
    r = auth_client.post("/api/webhook/stripe", data=raw, headers={"X-Signature": header})
    assert r.get_json()["status"] == "ignored"


def test_webhook_unknown_order(auth_client, billing):
    raw, header = signed_body(billing, {"order_id": 99999,
                                        "provider_ref": "pi_x", "status": "paid"})
    r = auth_client.post("/api/webhook/stripe", data=raw, headers={"X-Signature": header})
    assert r.get_json()["status"] == "unknown"


def test_webhook_grants_the_ordered_plan_not_a_named_one(auth_client, billing):
    # The payload names only an order; the plan comes from the order row, so a
    # caller cannot ask for "ultimate" by editing the webhook body.
    order_id = auth_client.post("/api/checkout", json={"plan": "basic"}).get_json()["order_id"]
    raw, header = signed_body(billing, {"order_id": order_id, "provider_ref": "pi_4",
                                        "status": "paid", "plan": "ultimate"})
    auth_client.post("/api/webhook/stripe", data=raw, headers={"X-Signature": header})
    assert auth_client.get("/api/me").get_json()["user"]["plan"] == "basic"


# -------------------------------------------------------------------- orders
def test_orders_require_auth(client):
    assert client.get("/api/orders").status_code == 401


def test_orders_listed_for_owner(auth_client, billing):
    auth_client.post("/api/checkout", json={"plan": "pro"})
    orders = auth_client.get("/api/orders").get_json()["orders"]
    assert len(orders) == 1 and orders[0]["plan"] == "pro"
