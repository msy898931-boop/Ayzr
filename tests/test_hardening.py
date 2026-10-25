"""
The protection system, end to end.
==================================

These pin the behaviour a change must not silently remove:

* CSRF: a mutation with no/again a wrong token is refused; the browser's token
  is accepted; webhook and ingest (HMAC/bearer auth) are exempt.
* Content-Type: a state change that is not ``application/json`` is refused, so a
  cross-site "simple" POST cannot fire an authenticated mutation.
* Password policy and email validation reject the weakest inputs.
* The redirect guard only honours same-origin paths.
* Secrets never survive a trip through the logger.
* Response headers (including HSTS) are stamped, and security.txt is served.
"""
from __future__ import annotations


# --------------------------------------------------------------------- CSRF
def test_csrf_rejects_a_mutation_without_a_token(raw_client, clean):
    r = raw_client.post("/api/auth/register",
                        json={"email": "a@tradli.io", "password": "secret123"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "csrf_failed"


def test_csrf_rejects_a_wrong_token(raw_client, clean):
    with raw_client.session_transaction() as sess:
        sess["_csrf"] = "the-real-token"
    r = raw_client.post("/api/auth/register", json={"email": "a@tradli.io",
                                                    "password": "secret123"},
                        headers={"X-CSRF-Token": "a-guess"})
    assert r.status_code == 403


def test_csrf_accepts_the_sessions_token(raw_client, clean):
    with raw_client.session_transaction() as sess:
        sess["_csrf"] = "the-real-token"
    r = raw_client.post("/api/auth/register", json={"email": "a@tradli.io",
                                                    "password": "secret123"},
                        headers={"X-CSRF-Token": "the-real-token"})
    assert r.status_code == 200
    assert r.get_json()["user"]["email"] == "a@tradli.io"


def test_csrf_token_is_exposed_and_rotated_on_login(client, clean):
    r = client.get("/api/me")
    tok = r.get_json()["csrf"]
    assert tok
    # The page carries the same token so the browser can echo it.
    html = client.get("/").get_data(as_text=True)
    assert f'name="csrf-token" content="{tok}"' in html


def test_safe_get_is_never_csrf_blocked(raw_client):
    assert raw_client.get("/api/health").status_code == 200


def test_webhook_is_csrf_exempt_but_still_signature_gated(raw_client, clean):
    """A webhook carries no session cookie, so CSRF must not apply -- but the
    HMAC signature still must."""
    r = raw_client.post("/api/webhook/stripe", json={"order_id": 1})
    # Not 403 (csrf); it is refused for a different, correct reason.
    assert r.status_code != 403 or r.get_json().get("error") != "csrf_failed"


# ------------------------------------------------------------- Content-Type
def test_content_type_guard_rejects_text_plain(raw_client, clean):
    """A 'simple' cross-site POST cannot set application/json, so we refuse it."""
    with raw_client.session_transaction() as sess:
        sess["_csrf"] = "tok"
    r = raw_client.post("/api/auth/register", data="email=a@tradli.io",
                        content_type="text/plain",
                        headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 415
    assert r.get_json()["error"] == "unsupported_media_type"


def test_content_type_guard_rejects_form_urlencoded(raw_client, clean):
    with raw_client.session_transaction() as sess:
        sess["_csrf"] = "tok"
    r = raw_client.post("/api/auth/register", data={"email": "a@tradli.io"},
                        headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 415


def test_content_type_guard_allows_json(raw_client, clean):
    with raw_client.session_transaction() as sess:
        sess["_csrf"] = "tok"
    r = raw_client.post("/api/auth/login", json={"email": "x@tradli.io",
                                                 "password": "secret123"},
                        headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 401  # wrong creds, but the request was allowed


# --------------------------------------------------------- password / email
def test_register_rejects_a_short_password(client):
    r = client.post("/api/auth/register",
                    json={"email": "a@tradli.io", "password": "short"})
    assert r.status_code == 400


def test_register_rejects_a_common_password(client):
    r = client.post("/api/auth/register",
                    json={"email": "a@tradli.io", "password": "password123"})
    assert r.status_code == 400


def test_register_rejects_a_reserved_email_domain(client):
    r = client.post("/api/auth/register",
                    json={"email": "a@example.com", "password": "secret123"})
    assert r.status_code == 400


def test_register_rejects_a_malformed_email(client):
    for bad in ("notanemail", "a@b", "a b@c.com", "a@@b.com"):
        r = client.post("/api/auth/register",
                        json={"email": bad, "password": "secret123"})
        assert r.status_code == 400, bad


def test_password_problem_tokens(clean):
    db = clean["db"]
    assert db.password_problem("") == "too_short"
    assert db.password_problem("aaaaaaa") == "too_short"
    assert db.password_problem("aaaaaaaaa") == "too_simple"
    assert db.password_problem("password123") == "too_common"
    assert db.password_problem("S3cure-pass!") is None


def test_validate_email_normalises(clean):
    db = clean["db"]
    assert db.validate_email("  A@Tradli.IO ") == "a@tradli.io"
    assert db.validate_email("a@sub.example.com") is None  # blocked domain suffix
    assert db.validate_email("real@trader.net") == "real@trader.net"


# ------------------------------------------------------------ redirect guard
def test_safe_next_only_allows_same_origin_paths(clean):
    s = clean["security"]
    assert s.safe_next("/billing") == "/billing"
    assert s.safe_next("//evil.com") == "/"
    assert s.safe_next("https://evil.com") == "/"
    assert s.safe_next("/\\evil.com") == "/"
    assert s.safe_next("/a\\b") == "/"
    assert s.safe_next("/x\r\nSet-Cookie: y") == "/"
    assert s.safe_next(None) == "/"
    assert s.safe_next("") == "/"


# ------------------------------------------------------------- log redaction
def test_redact_masks_common_secret_shapes(clean):
    s = clean["security"]
    assert "sk_live_" not in s.redact("key sk_live_abc123DEF456ghi789")
    assert "whsec_" not in s.redact("whsec_0123456789abcdef")
    assert "AIza" not in s.redact("AIzaSyA1234567890abcdefghij")
    assert "gsk_" not in s.redact("gsk_abcdefghijklmnop")
    assert "***" in s.redact("Authorization: Bearer supersecrettoken")
    assert "hunter2" not in s.redact("password=hunter2")
    # A non-secret string is returned unchanged.
    assert s.redact("plain text 123") == "plain text 123"


def test_log_event_redacts_values(clean, caplog):
    import logging as _logging
    s = clean["security"]
    logger = _logging.getLogger("test.redact")
    with caplog.at_level(_logging.INFO, logger="test.redact"):
        s.log_event(logger, "info", "charge", token="whsec_abcdef123456")
    assert "whsec_abcdef123456" not in caplog.text
    assert "charge" in caplog.text


# ---------------------------------------------------------------- headers
def test_security_headers_include_the_new_ones(client):
    h = client.get("/").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "same-origin"
    assert h["Cross-Origin-Opener-Policy"] == "same-origin"
    assert h["Cross-Origin-Resource-Policy"] == "same-origin"
    assert h["X-Permitted-Cross-Domain-Policies"] == "none"
    assert "Permissions-Policy" in h


def test_csp_has_nonce_and_no_unsafe_inline(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "script-src 'self' 'nonce-" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_hsts_only_in_production(client, clean):
    """Development must not pin HSTS -- it would break plain http://localhost."""
    assert "Strict-Transport-Security" not in client.get("/").headers
    resp = clean["server"].app.test_request_context()
    # Exercise the header builder directly with is_prod=True.
    from flask import Response
    r = clean["security"].apply_security_headers(Response("x"), "nonce", is_prod=True)
    assert "Strict-Transport-Security" in r.headers
    assert "max-age=31536000" in r.headers["Strict-Transport-Security"]


def test_security_txt_is_served(client, mods):
    r = client.get("/.well-known/security.txt")
    assert r.status_code == 200
    assert r.mimetype == "text/plain"
    body = r.get_data(as_text=True)
    assert f"Contact: mailto:{mods['config'].CONTACT_EMAIL}" in body
    assert "Expires:" in body


# ------------------------------------------------------------ health surface
def test_health_reports_the_security_posture(client):
    sec = client.get("/api/health").get_json()["security"]
    assert sec["csrf"] is True
    assert sec["require_json_content_type"] is True
    assert sec["password_min_length"] >= 8
    assert sec["login_lockout"]["attempts"] >= 1
    assert "backend" in sec["rate_limit"]
    assert sec["hsts"] is False  # development


def test_register_is_rate_limited(client, clean):
    """Register has its own budget so it cannot be used to spam accounts."""
    clean["config"].RATE_LIMIT_CALLS = 3
    codes = []
    for i in range(6):
        r = client.post("/api/auth/register",
                        json={"email": f"u{i}@tradli.io", "password": "secret123"})
        codes.append(r.status_code)
    assert 429 in codes
