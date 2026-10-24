"""
Subscriptions: a paid plan is a term, not a permanent grant.
================================================================

The whole point of a subscription is that it *ends*. These tests pin the three
rules that make that safe:

  1. a paid plan grants `days` of access from the moment it is activated;
  2. once the term lapses the user is treated as free -- with the free lifetime
     allowance, not the paid daily one;
  3. renewing before expiry extends the term instead of resetting it.
"""
from __future__ import annotations

import time

DAY = 86400


def _user(db, email="sub@x.com"):
    return db.create_user(email, "secret123")["id"]


def test_activate_order_grants_a_term(clean):
    db = clean["db"]
    uid = _user(db)
    oid = db.create_order(uid, "pro", 59.99)
    res = db.activate_order(oid, "stripe", "pi_term")
    assert res["status"] == "activated"
    row = db.get_user(uid)
    assert row["plan"] == "pro"
    assert res["expires"] is not None
    # 30 days, give or take the second it took to run.
    assert abs(res["expires"] - (time.time() + 30 * DAY)) < 5


def test_plan_is_active_before_expiry(clean):
    db = clean["db"]
    uid = _user(db)
    db.activate_order(db.create_order(uid, "basic", 39.99), "stripe", "pi_a")
    assert db.plan_active(uid) is True
    assert db.effective_plan(db.get_user(uid)) == "basic"
    assert db.days_left(db.get_user(uid)) == 30


def test_plan_lapses_after_expiry(clean):
    db = clean["db"]
    uid = _user(db)
    db.set_plan(uid, "pro", days=30, now=time.time() - 31 * DAY)
    # Stored plan is still "pro", but the user is no longer entitled to it.
    assert db.get_user(uid)["plan"] == "pro"
    assert db.plan_active(uid) is False
    assert db.effective_plan(db.get_user(uid)) == "free"
    assert db.days_left(db.get_user(uid)) == 0


def test_expired_subscription_falls_back_to_free_limits(clean):
    db = clean["db"]
    uid = _user(db)
    db.set_plan(uid, "ultimate", days=30, now=time.time() - 31 * DAY)
    q = db.quota(uid, "analyze")
    assert q["plan"] == "free"
    assert q["limit"] == 1  # free lifetime allowance, not ultimate's 15/day
    assert q["used"] == 0


def test_consume_uses_free_quota_once_expired(clean):
    db = clean["db"]
    uid = _user(db)
    db.set_plan(uid, "pro", days=30, now=time.time() - 31 * DAY)
    assert db.consume(uid, "analyze") is True   # the one free credit
    assert db.consume(uid, "analyze") is False  # and that is all
    # zones is paid-only; an expired subscriber must not get it.
    assert db.consume(uid, "zones") is False


def test_renewal_extends_from_current_expiry(clean):
    db = clean["db"]
    uid = _user(db)
    first = db.activate_order(db.create_order(uid, "pro", 59.99), "stripe", "pi_1")
    second = db.activate_order(db.create_order(uid, "pro", 59.99), "stripe", "pi_2")
    # Buying again while active adds 30 days on top of the remaining term.
    assert second["expires"] > first["expires"]
    assert abs(second["expires"] - (first["expires"] + 30 * DAY)) < 5


def test_renewal_after_lapse_restarts_from_now(clean):
    db = clean["db"]
    uid = _user(db)
    db.set_plan(uid, "pro", days=30, now=time.time() - 40 * DAY)  # long expired
    res = db.activate_order(db.create_order(uid, "pro", 59.99), "stripe", "pi_3")
    assert abs(res["expires"] - (time.time() + 30 * DAY)) < 5


def test_switching_plan_starts_a_fresh_term(clean):
    db = clean["db"]
    uid = _user(db)
    db.activate_order(db.create_order(uid, "basic", 39.99), "stripe", "pi_b")
    res = db.activate_order(db.create_order(uid, "pro", 59.99), "stripe", "pi_p")
    row = db.get_user(uid)
    assert row["plan"] == "pro"
    assert abs(res["expires"] - (time.time() + 30 * DAY)) < 5  # not stacked


def test_me_reports_subscription_state(auth_client, clean):
    clean["db"].set_plan(clean["uid"], "pro", days=30)
    u = auth_client.get("/api/me").get_json()["user"]
    assert u["plan"] == "pro"
    assert u["active"] is True
    assert u["days_left"] == 30
    assert u["plan_expires"] is not None


def test_me_reports_free_after_expiry(auth_client, clean):
    clean["db"].set_plan(clean["uid"], "pro", days=30, now=time.time() - 31 * DAY)
    body = auth_client.get("/api/me").get_json()
    assert body["user"]["plan"] == "free"
    assert body["user"]["active"] is False
    assert body["quotas"]["analyze"]["plan"] == "free"


def test_admin_stats_counts_lapsed_subscriptions(client, clean, monkeypatch):
    monkeypatch.setattr(clean["config"], "ADMIN_TOKEN", "tok")
    db = clean["db"]
    live = _user(db, "live@x.com")
    dead = _user(db, "dead@x.com")
    db.set_plan(live, "pro", days=30)
    db.set_plan(dead, "pro", days=30, now=time.time() - 31 * DAY)
    body = client.get("/api/admin/stats",
                      headers={"Authorization": "Bearer tok"}).get_json()
    assert body["users"]["expired"] == 1
    assert body["users"]["total"] == 2
