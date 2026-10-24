"""
HTTP layer: auth, quotas, rate limiting, login throttling, image intake.
=======================================================================

These are the checks a buyer's security review will actually run.
"""
from __future__ import annotations

import base64

from conftest import register


def png_url() -> str:
    import struct
    import zlib

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))
    w, h = 8, 6
    rows = b"".join(b"\x00" + bytes([10, 20, 30]) * w for _ in range(h))
    raw = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows))
           + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(raw).decode()


# ------------------------------------------------------------------- basics
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["agents"] == 12
    assert body["live"] is False  # no key configured in tests
    assert body["env"] == "development"
    assert body["version"] == "1.7.0"


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Tradli" in r.data or b"\xd8\xaa" in r.data  # brand or arabic text


def test_plans_are_public(client):
    body = client.get("/api/plans").get_json()
    assert set(body["plans"]) == {"free", "basic", "pro", "ultimate"}


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"


def test_apk_download_404_when_not_built(client, clean, tmp_path, monkeypatch):
    monkeypatch.setattr(clean["server"], "DOWNLOAD_DIR", str(tmp_path))
    r = client.get("/api/app")
    assert r.status_code == 404
    assert r.get_json()["error"] == "app not built"


def test_apk_download_serves_built_apk(client, clean, tmp_path, monkeypatch):
    (tmp_path / "AuroraMarkets.apk").write_bytes(b"PK\x03\x04fake-apk")
    monkeypatch.setattr(clean["server"], "DOWNLOAD_DIR", str(tmp_path))
    r = client.get("/api/app")
    assert r.status_code == 200
    assert r.mimetype == "application/vnd.android.package-archive"
    assert r.data.startswith(b"PK")
    r.close()


# --------------------------------------------------------------------- auth
def test_register_and_me(client):
    r = register(client, "a@b.com", "secret123")
    assert r.status_code == 200
    me = client.get("/api/me").get_json()
    assert me["user"]["email"] == "a@b.com"
    assert me["user"]["plan"] == "free"


def test_register_rejects_weak_password(client):
    assert register(client, "a@b.com", "123").status_code == 400


def test_register_rejects_bad_email(client):
    assert register(client, "notanemail", "secret123").status_code == 400


def test_register_rejects_duplicate(client):
    register(client, "dup@b.com", "secret123")
    assert register(client, "dup@b.com", "secret123").status_code == 400


def test_email_is_case_insensitive(client):
    register(client, "Mixed@Case.com", "secret123")
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": "mixed@case.com",
                                             "password": "secret123"})
    assert r.status_code == 200


def test_login_wrong_password(client):
    register(client, "a@b.com", "secret123")
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": "a@b.com", "password": "nope"})
    assert r.status_code == 401


def test_logout_clears_session(auth_client):
    assert auth_client.get("/api/me").get_json()["user"] is not None
    auth_client.post("/api/auth/logout")
    assert auth_client.get("/api/me").get_json()["user"] is None


def test_password_is_not_stored_in_plaintext(clean):
    db = clean["db"]
    db.create_user("x@y.com", "secret123")
    row = db.get_user_by_email("x@y.com")
    assert "secret123" not in row["pw_hash"]
    assert len(row["pw_hash"]) == 64  # sha256 hex


def test_password_verify_roundtrip(clean):
    db = clean["db"]
    db.create_user("x@y.com", "secret123")
    row = db.get_user_by_email("x@y.com")
    assert db.verify_pw("secret123", row["pw_hash"], row["salt"]) is True
    assert db.verify_pw("wrong", row["pw_hash"], row["salt"]) is False


# --------------------------------------------------------- login throttling
def test_login_locks_out_after_repeated_failures(client):
    register(client, "a@b.com", "secret123")
    client.post("/api/auth/logout")
    codes = []
    for _ in range(12):
        r = client.post("/api/auth/login",
                        json={"email": "a@b.com", "password": "bad"})
        codes.append(r.status_code)
    assert 429 in codes  # eventually locked, not an endless 401 stream


def test_lockout_blocks_even_correct_password(client):
    register(client, "a@b.com", "secret123")
    client.post("/api/auth/logout")
    for _ in range(10):
        client.post("/api/auth/login", json={"email": "a@b.com", "password": "bad"})
    r = client.post("/api/auth/login", json={"email": "a@b.com", "password": "secret123"})
    assert r.status_code == 429


def test_successful_login_clears_failures(client):
    register(client, "a@b.com", "secret123")
    client.post("/api/auth/logout")
    for _ in range(3):
        client.post("/api/auth/login", json={"email": "a@b.com", "password": "bad"})
    assert client.post("/api/auth/login",
                       json={"email": "a@b.com", "password": "secret123"}).status_code == 200
    # Counter reset: three more failures should not lock us out.
    client.post("/api/auth/logout")
    for _ in range(3):
        client.post("/api/auth/login", json={"email": "a@b.com", "password": "bad"})
    assert client.post("/api/auth/login",
                       json={"email": "a@b.com", "password": "secret123"}).status_code == 200


# --------------------------------------------------------------- rate limit
def test_rate_limit_kicks_in(client, monkeypatch):
    monkeypatch.setattr(client.application.config["TESTING"] and __import__("config"),
                        "RATE_LIMIT_CALLS", 5, raising=False)
    codes = [client.get("/api/quant").status_code for _ in range(12)]
    assert 429 in codes


# ------------------------------------------------------------------- quotas
def test_features_require_auth(client):
    for path in ("/api/analyze", "/api/verify", "/api/advise", "/api/zones"):
        assert client.post(path, json={"image": png_url()}).status_code == 401
    assert client.get("/api/history").status_code == 401


def test_analyze_consumes_free_quota(auth_client):
    r = auth_client.post("/api/analyze", json={"image": png_url(), "symbol": "XAUUSD"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["result"]["live"] is False  # demo path, honestly labelled
    assert body["quota"]["remaining"] == 0  # free plan = 1 analysis


def test_second_analyze_is_blocked_on_free_plan(auth_client):
    auth_client.post("/api/analyze", json={"image": png_url()})
    r = auth_client.post("/api/analyze", json={"image": png_url()})
    assert r.status_code == 402
    assert r.get_json()["error"] == "quota_exhausted"


def test_paid_plan_allows_more(auth_client, clean):
    # A plan is only granted by a verified payment now; grant it directly in the
    # DB (as the billing layer would) instead of via the closed /api/plan route.
    clean["db"].set_plan(clean["uid"], "pro")
    codes = [auth_client.post("/api/analyze", json={"image": png_url()}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 200]


def test_zones_unavailable_on_free_plan(auth_client):
    r = auth_client.post("/api/zones", json={"image": png_url()})
    assert r.status_code == 402


def test_bad_image_does_not_spend_a_credit(auth_client):
    # A rejected upload must not cost the user their one free analysis.
    r = auth_client.post("/api/analyze", json={"image": "data:image/png;base64,zzz"})
    assert r.status_code == 400
    assert auth_client.get("/api/me").get_json()["quotas"]["analyze"]["remaining"] == 1
    # ... and the real analysis still works afterwards.
    assert auth_client.post("/api/analyze",
                            json={"image": png_url()}).status_code == 200


def test_engine_failure_refunds_the_credit(auth_client, clean, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(clean["server"].engine, "analyze", boom)
    r = auth_client.post("/api/analyze", json={"image": png_url()})
    assert r.status_code == 500
    assert auth_client.get("/api/me").get_json()["quotas"]["analyze"]["remaining"] == 1


def test_consume_is_atomic_under_concurrency(clean):
    # More threads than credits race at once; only the limit may win.
    import threading

    db = clean["db"]
    uid = db.create_user("race@example.com", "secret123")["id"]
    db.set_plan(uid, "basic")  # basic = 3 analyze/day
    limit = db.plan_limits("basic")["analyze"]

    wins = []
    lock = threading.Lock()

    def worker():
        ok = db.consume(uid, "analyze")
        with lock:
            wins.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(limit * 4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == limit
    assert db.quota(uid, "analyze")["used"] == limit


def test_history_records_analyses(auth_client):
    auth_client.post("/api/analyze", json={"image": png_url(), "symbol": "XAUUSD"})
    hist = auth_client.get("/api/history").get_json()["history"]
    assert len(hist) == 1
    assert hist[0]["feature"] == "analyze"
    assert hist[0]["symbol"] == "XAUUSD"


def test_history_is_per_user(client):
    register(client, "one@b.com", "secret123")
    client.post("/api/analyze", json={"image": png_url()})
    client.post("/api/auth/logout")
    register(client, "two@b.com", "secret123")
    assert client.get("/api/history").get_json()["history"] == []


def test_bad_image_is_rejected(auth_client):
    r = auth_client.post("/api/analyze", json={"image": "not-an-image"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_image"


def test_plan_switch_is_disabled(auth_client):
    # Regression guard: this route used to hand out any plan for free.
    r = auth_client.post("/api/plan", json={"plan": "ultimate"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "use_checkout"
    assert auth_client.get("/api/me").get_json()["user"]["plan"] == "free"


# ------------------------------------------------------------------- market
def test_markets_are_public(client):
    body = client.get("/api/markets").get_json()
    assert any(s["symbol"] == "XAUUSD" for s in body["symbols"])
    assert "H1" in body["timeframes"]


def test_signals_are_seeded(client):
    body = client.get("/api/signals").get_json()
    assert len(body["signals"]) >= 1
    assert {"channel", "symbol", "direction"} <= set(body["signals"][0])
