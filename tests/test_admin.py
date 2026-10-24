"""
Admin API: aggregate stats behind a bearer token.
=================================================

The admin surface is read-only and fails closed: with no token configured it is
off (503), and with a token configured a wrong one is rejected (403) in
constant time.
"""
from __future__ import annotations

import pytest

TOKEN = "admin-token-xyz"


@pytest.fixture()
def admin_on(clean, monkeypatch):
    monkeypatch.setattr(clean["config"], "ADMIN_TOKEN", TOKEN)
    return clean


def test_admin_off_by_default(client):
    assert client.get("/api/admin/stats").status_code == 503


def test_admin_rejects_missing_token(client, admin_on):
    assert client.get("/api/admin/stats").status_code == 403


def test_admin_rejects_wrong_token(client, admin_on):
    r = client.get("/api/admin/stats", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_admin_accepts_correct_token(client, admin_on):
    r = client.get("/api/admin/stats", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert set(r.get_json()) == {"users", "orders", "recent_orders"}


def test_admin_page_renders_without_secrets(client):
    """The /admin shell is public: it must render and must not embed the token."""
    r = client.get("/admin")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "لوحة الإدارة" in body
    # The page asks for the token; it never carries it.
    assert "ADMIN_TOKEN" in body


def test_admin_stats_counts_users_by_plan(client, admin_on):
    client.post("/api/auth/register", json={"email": "a@x.com", "password": "secret123"})
    client.post("/api/auth/register", json={"email": "b@x.com", "password": "secret123"})
    body = client.get("/api/admin/stats",
                      headers={"Authorization": f"Bearer {TOKEN}"}).get_json()
    assert body["users"]["total"] == 2
    assert body["users"]["by_plan"].get("free") == 2


def test_admin_stats_revenue_only_counts_paid(client, admin_on, clean):
    db = clean["db"]
    uid = db.create_user("buyer@x.com", "secret123")["id"]
    db.create_order(uid, "pro", 59.99)
    db.create_order(uid, "basic", 39.99)
    body = client.get("/api/admin/stats",
                      headers={"Authorization": f"Bearer {TOKEN}"}).get_json()
    assert body["orders"]["total"] == 2
    assert body["orders"]["paid"] == 0
    assert body["orders"]["revenue"] == 0


def test_admin_revenue_increases_after_payment(client, admin_on, clean, monkeypatch):
    cfg, db = clean["config"], clean["db"]
    monkeypatch.setattr(cfg, "PAYMENT_PROVIDER", "stripe")
    monkeypatch.setattr(cfg, "PAYMENT_KEY", "sk_test")
    monkeypatch.setattr(cfg, "PAYMENT_WEBHOOK_SECRET", "whsec_test")
    uid = db.create_user("pay@x.com", "secret123")["id"]
    oid = db.create_order(uid, "pro", 59.99)
    db.activate_order(oid, "stripe", "pi_1")
    body = client.get("/api/admin/stats",
                      headers={"Authorization": f"Bearer {TOKEN}"}).get_json()
    assert body["orders"]["paid"] == 1
    assert body["orders"]["revenue"] == 59.99
    assert body["users"]["by_plan"].get("pro") == 1
    assert body["recent_orders"][0]["email"] == "pay@x.com"
