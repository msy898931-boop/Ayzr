"""
Market analysis platform -- web backend.
=========================================================

    GET  /                     landing + web app (single page)
    GET  /api/health           provider + build info
    GET  /api/plans            subscription plans
    GET  /api/signals          aggregated signals hub
    POST /api/auth/register    create account
    POST /api/auth/login       sign in
    POST /api/auth/logout      sign out
    GET  /api/me               current user + quotas
    POST /api/analyze          chart analysis   (image)
    POST /api/verify           fraud detector   (image)
    POST /api/advise           position advisor (image)
    POST /api/zones            supply & demand  (image)
    GET  /api/history          this user's past analyses

Run:  PORT=8000 python app/server.py
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time

from flask import Flask, g, jsonify, render_template, request, send_file, session

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(path: str) -> None:
    """Minimal .env loader so keys survive without extra dependencies."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import logging  # noqa: E402

import agents as roster  # noqa: E402
import billing  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import engine  # noqa: E402
import imaging  # noqa: E402
import llm  # noqa: E402
import security  # noqa: E402

ROOT = config.ROOT
DOWNLOAD_DIR = os.path.join(ROOT, "dist")

# One structured logger for the events an operator actually needs to see:
# sign-ins, payment settlements and refusals. Request-level noise stays with
# gunicorn's access log; this is the audit trail.
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = config.secret_key()
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

# Session cookies: not readable from JS, not sent on cross-site requests.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.IS_PROD,
)

db.init()
db.seed_signals()


# ------------------------------------------------------------------ helpers
def client_ip() -> str:
    """Best-effort caller identity for rate limiting and login lockout."""
    if config.TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


# Install the request-level protections (CSP nonce + CSRF token minting, and
# the security headers) *before* the guard below, so Flask runs the minting
# ``before_request`` first and the token exists by the time the guard checks it.
security.init_app(app)


@app.before_request
def _guard_requests():
    """Reject cross-site state changes before any view runs.

    Order matters: the Content-Type guard is checked first (a cheap header
    test), then the CSRF token. Both only apply to state-changing methods on
    non-exempt paths. This runs after ``security.init_app``'s hook, which has
    already minted the session's CSRF token.
    """
    if not security.json_content_type_ok():
        return jsonify({"error": "unsupported_media_type",
                        "detail": "state-changing requests must be application/json"}), 415
    if not security.csrf_ok():
        log.warning("csrf rejected path=%s ip=%s", request.path, client_ip())
        return jsonify({"error": "csrf_failed",
                        "detail": "missing or invalid CSRF token"}), 403



def current_uid() -> int | None:
    return session.get("uid")


def _image_from_request() -> tuple[bytes, str]:
    """Accept a JSON data-URL body or a multipart file upload."""
    if request.files.get("image"):
        f = request.files["image"]
        raw = f.read()
        mime = f.mimetype or "image/jpeg"
        if len(raw) > imaging.MAX_BYTES:
            raise imaging.ImageError("image too large (max 8 MB)")
        return raw, imaging.validate_image(raw, mime)
    body = request.get_json(silent=True) or {}
    mime, raw = imaging.parse_data_url(body.get("image", ""))
    return raw, mime


def _feature_call(feature: str, fn):
    """Shared plumbing: rate limit, auth, quota, run, log, respond.

    Order matters for money: we validate the image *before* spending a credit
    and refund it if the engine fails, so a user is only ever charged for a
    successful run.
    """
    if not security.allow(f"ip:{client_ip()}"):
        return jsonify({"error": "rate_limited",
                        "detail": "too many requests, slow down"}), 429
    uid = current_uid()
    if uid is None:
        return jsonify({"error": "auth_required"}), 401
    if not db.consume(uid, feature):
        q = db.quota(uid, feature)
        return jsonify({"error": "quota_exhausted", "quota": q}), 402
    try:
        image, mime = _image_from_request()
    except imaging.ImageError as exc:
        db.refund(uid, feature)
        return jsonify({"error": "bad_image", "detail": str(exc)}), 400
    try:
        result = fn(image, mime)
    except Exception as exc:  # noqa: BLE001
        db.refund(uid, feature)
        return jsonify({"error": "engine_failed", "detail": str(exc)}), 500
    body = request.get_json(silent=True) or {}
    db.save_history(uid, feature, result,
                    body.get("symbol", ""), body.get("timeframe", ""))
    return jsonify({"result": result, "quota": db.quota(uid, feature)})


# ------------------------------------------------------------------- pages
@app.route("/")
def index():
    return render_template("index.html",
                           plans=db.PLANS,
                           agents=roster.roster(),
                           provider=llm.provider_name(),
                           model=llm.model_name(),
                           brand=config.BRAND_NAME,
                           csrf_token=security.csrf_token(),
                           csp_nonce=g.csp_nonce)


@app.route("/terms")
def terms():
    return render_template("legal.html", doc="terms", brand=config.BRAND_NAME,
                           contact=config.CONTACT_EMAIL, csp_nonce=g.csp_nonce,
                           version=config.VERSION)


@app.route("/privacy")
def privacy():
    return render_template("legal.html", doc="privacy", brand=config.BRAND_NAME,
                           contact=config.CONTACT_EMAIL, csp_nonce=g.csp_nonce,
                           version=config.VERSION)


@app.route("/admin")
def admin_page():
    """The dashboard shell. The data itself still requires the bearer token;
    this page holds no secrets and is safe to serve to anyone."""
    return render_template("admin.html", brand=config.BRAND_NAME,
                           csp_nonce=g.csp_nonce)


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "provider": llm.provider_name(),
        "model": llm.model_name(),
        "live": llm.has_live_model(),
        "agents": len(roster.ALL_AGENTS),
        "providers": llm.configured_providers(),
        "env": config.ENV,
        "version": config.VERSION,
        "billing": config.billing_configured(),
        "rate_limit_backend": security.backend_name(),
        "brand": config.BRAND_NAME,
        "security": config.security_status(),
    })


@app.route("/.well-known/security.txt")
def security_txt():
    """RFC 9116 contact info for security researchers.

    Plain text, one field per line. The contact address is the operator's, set
    via CONTACT_EMAIL -- there is no hard-coded mailbox to rot.
    """
    body = (
        f"Contact: mailto:{config.CONTACT_EMAIL}\n"
        f"Expires: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 31536000))}\n"
        "Preferred-Languages: ar, en\n"
        "Canonical: /.well-known/security.txt\n"
    )
    return app.response_class(body, mimetype="text/plain")


@app.route("/api/providers")
def api_providers():
    """Which configured keys actually answer right now."""
    return jsonify({"results": llm.probe_all(),
                    "active": llm.provider_name()})


@app.route("/api/download")
def api_download():
    """Serve the portable zip if it has been built, else explain how to build."""
    path = os.path.join(DOWNLOAD_DIR, "Market-AI-Platform.zip")
    if not os.path.isfile(path):
        return jsonify({"ok": False,
                        "error": "package not built",
                        "hint": "run ./make-portable.sh (or ./run.sh zip) on the host"}), 404
    return send_file(path, as_attachment=True,
                     download_name="Market-AI-Platform.zip")


@app.route("/api/app")
def api_app():
    """Serve the Android APK if it has been built, else explain how to build."""
    path = os.path.join(DOWNLOAD_DIR, "AuroraMarkets.apk")
    if not os.path.isfile(path):
        return jsonify({"ok": False,
                        "error": "app not built",
                        "hint": "run android/build.sh on the host"}), 404
    return send_file(path, as_attachment=True,
                     download_name="AuroraMarkets.apk",
                     mimetype="application/vnd.android.package-archive")


@app.route("/api/plans")
def api_plans():
    return jsonify({"plans": db.PLANS,
                    "features": {k: {"en": v[0], "ar": v[1]}
                                 for k, v in db.FEATURE_LABEL.items()}})


@app.route("/api/signals")
def api_signals():
    try:
        max_age = int(request.args.get("max_age", 0))
    except (TypeError, ValueError):
        max_age = 0
    return jsonify({"signals": db.list_signals(max_age or None)})


@app.route("/api/signals/ingest", methods=["POST"])
def api_signals_ingest():
    """Add a signal from an authenticated feed.

    Off by default (503 without a token). A bearer token is required and checked
    in constant time, and the payload is validated in `db.add_signal` -- the
    public read endpoint must never become a way to write arbitrary markup into
    every visitor's screen.
    """
    if not config.ingest_configured():
        return jsonify({"error": "ingest_not_configured",
                        "detail": "set SIGNAL_INGEST_TOKEN to accept signals"}), 503
    if not security.allow(f"ingest:{client_ip()}"):
        return jsonify({"error": "rate_limited"}), 429
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token or not secrets.compare_digest(token, config.SIGNAL_INGEST_TOKEN):
        return jsonify({"error": "forbidden"}), 403
    b = request.get_json(silent=True) or {}
    row = db.add_signal(b.get("channel", ""), b.get("symbol", ""),
                        b.get("direction", ""), b.get("entry"), b.get("stop"),
                        b.get("targets", []), b.get("credibility", 50))
    if row is None:
        return jsonify({"error": "invalid_signal"}), 400
    log.info("signal ingested id=%s channel=%s symbol=%s",
             row["id"], row["channel"], row["symbol"])
    return jsonify({"ok": True, "signal": row}), 201


# -------------------------------------------------------------------- auth
@app.route("/api/auth/register", methods=["POST"])
def api_register():
    if not security.allow(f"register:{client_ip()}"):
        return jsonify({"error": "rate_limited",
                        "detail": "too many requests, slow down"}), 429
    b = request.get_json(silent=True) or {}
    user = db.create_user(b.get("email", ""), b.get("password", ""))
    if not user:
        return jsonify({"error": "invalid_or_taken"}), 400
    session["uid"] = user["id"]
    session.pop(security.CSRF_SESSION_KEY, None)  # rotate the token on privilege change
    log.info("register ok ip=%s uid=%s", client_ip(), user["id"])
    return jsonify({"user": user, "csrf": security.csrf_token()})


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    ip = client_ip()
    wait = security.locked(f"login:{ip}")
    if wait:
        return jsonify({"error": "locked", "retry_after": wait}), 429
    b = request.get_json(silent=True) or {}
    row = db.get_user_by_email(b.get("email", ""))
    if not row or not db.verify_pw(b.get("password", ""), row["pw_hash"], row["salt"]):
        security.record_failure(f"login:{ip}")
        log.warning("login failed ip=%s email=%s", ip, b.get("email", ""))
        return jsonify({"error": "bad_credentials"}), 401
    security.record_success(f"login:{ip}")
    session.clear()               # fresh session on privilege change
    session["uid"] = row["id"]
    log.info("login ok ip=%s uid=%s", ip, row["id"])
    return jsonify({"user": {"id": row["id"], "email": row["email"], "plan": row["plan"]},
                    "csrf": security.csrf_token()})


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    uid = current_uid()
    tok = security.csrf_token()
    if uid is None:
        return jsonify({"user": None, "csrf": tok})
    row = db.get_user(uid)
    if not row:
        session.pop("uid", None)
        return jsonify({"user": None, "csrf": tok})
    quotas = {f: db.quota(uid, f) for f in db.FEATURE_LABEL}
    plan = db.effective_plan(row)
    return jsonify({"user": {"id": row["id"], "email": row["email"],
                             "plan": plan,
                             "plan_expires": row["plan_expires"],
                             "days_left": db.days_left(row),
                             "active": db.plan_active(uid),
                             "auto_renew": db.auto_renewing(uid)},
                    "quotas": quotas,
                    "csrf": tok})


@app.route("/api/plan", methods=["POST"])
def api_set_plan():
    """Self-service plan switching is disabled.

    Granting a plan requires a verified payment (see /api/checkout and
    /api/webhook). This endpoint used to let any signed-in user promote
    themselves to any plan for free, which made the product impossible to sell.
    """
    return jsonify({
        "error": "use_checkout",
        "detail": "plans are granted by a verified payment, not by request",
    }), 403


# ------------------------------------------------------------------ billing
@app.route("/api/checkout", methods=["POST"])
def api_checkout():
    """Start a purchase. Returns where to send the buyer, or a clear refusal."""
    uid = current_uid()
    if uid is None:
        return jsonify({"error": "auth_required"}), 401
    if not config.billing_configured():
        return jsonify({"error": "billing_not_configured",
                        "detail": "set PAYMENT_PROVIDER and PAYMENT_KEY to sell plans"}), 503
    b = request.get_json(silent=True) or {}
    plan = str(b.get("plan", "")).strip().lower()
    if plan not in db.PLANS or plan == "free":
        return jsonify({"error": "unknown_plan"}), 400
    auto_renew = bool(b.get("auto_renew"))
    amount = db.PLANS[plan]["price"]
    order_id = db.create_order(uid, plan, amount, config.PAYMENT_PROVIDER,
                               auto_renew=auto_renew)
    return jsonify({"order_id": order_id, "plan": plan, "amount": amount,
                    "auto_renew": auto_renew,
                    "checkout_url": billing.checkout_url(plan)})


@app.route("/api/subscription/cancel", methods=["POST"])
def api_cancel_subscription():
    """Turn off automatic renewal. The current term is left intact."""
    uid = current_uid()
    if uid is None:
        return jsonify({"error": "auth_required"}), 401
    result = billing.cancel_subscription(uid)
    if result["status"] == "none":
        return jsonify({"error": "no_subscription"}), 404
    # Best-effort: ask the provider to stop billing. The local switch above is
    # what we rely on, so a provider hiccup never leaves the user still enrolled.
    if config.billing_configured():
        billing.provider_cancel(result.get("provider_sub", ""))
    return jsonify(result)


@app.route("/api/orders")
def api_orders():
    uid = current_uid()
    if uid is None:
        return jsonify({"error": "auth_required"}), 401
    return jsonify({"orders": db.list_orders(uid)})


@app.route("/api/billing/history")
def api_billing_history():
    """The customer's own invoice view: subscription state + every payment.

    Read-only and scoped to the caller -- an order carries an amount and a
    timestamp, so this must never be reachable without a session.
    """
    uid = current_uid()
    if uid is None:
        return jsonify({"error": "auth_required"}), 401
    user = db.get_user(uid)
    orders = db.list_orders(uid, limit=50)
    paid = [o for o in orders if o["status"] == "paid"]
    return jsonify({
        "subscription": {
            "plan": db.effective_plan(user),
            "active": db.plan_active(uid),
            "days_left": db.days_left(user),
            "plan_expires": user["plan_expires"],
            "auto_renew": db.auto_renewing(uid),
        },
        "totals": {
            "count": len(paid),
            "spent": round(sum(o["amount"] for o in paid), 2),
        },
        "orders": orders,
    })


@app.route("/api/webhook/<provider>", methods=["POST"])
def api_webhook(provider: str):
    """Payment provider callback.

    Authentication comes from the HMAC signature over the raw body, never from
    the URL. Without a configured secret we refuse (503) rather than trust the
    caller -- otherwise anyone could POST a fake "paid" and grant themselves a
    plan.
    """
    if not config.webhook_configured():
        return jsonify({"error": "webhook_not_configured"}), 503
    raw = request.get_data()
    header = (request.headers.get("X-Signature")
              or request.headers.get("Stripe-Signature")
              or request.headers.get("X-Webhook-Signature") or "")
    if not billing.verify_signature(raw, header):
        return jsonify({"error": "bad_signature"}), 401
    # Parse the raw body ourselves: the signature is over these exact bytes, and
    # some providers omit the JSON content type.
    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return jsonify({"error": "bad_payload"}), 400
    result = billing.settle(payload)
    code = 200 if result.get("status") in ("activated", "already", "ignored") else 400
    log.info("webhook provider=%s status=%s result=%s",
             provider, result.get("status"), result)
    return jsonify(result), code


# -------------------------------------------------------------------- admin
def _admin_ok() -> bool:
    """Bearer-token check for the admin endpoints.

    Fails closed when no token is configured. The comparison is constant-time
    so the token cannot be guessed byte by byte, and the shared rate limiter
    caps how many attempts an IP gets.
    """
    if not config.admin_configured():
        return False
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return bool(token) and secrets.compare_digest(token, config.ADMIN_TOKEN)


@app.route("/api/admin/stats")
def api_admin_stats():
    if not config.admin_configured():
        return jsonify({"error": "admin_not_configured",
                        "detail": "set ADMIN_TOKEN to enable the admin API"}), 503
    if not security.allow(f"admin:{client_ip()}"):
        return jsonify({"error": "rate_limited"}), 429
    if not _admin_ok():
        return jsonify({"error": "forbidden"}), 403
    return jsonify(db.stats())


# ----------------------------------------------------------------- features
@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    b = request.get_json(silent=True) or {}

    def run(image, mime):
        return engine.analyze(image, mime, b.get("symbol", ""),
                              b.get("timeframe", ""), b.get("mode", "fast"),
                              b.get("note", ""))

    return _feature_call("analyze", run)


@app.route("/api/verify", methods=["POST"])
def api_verify():
    b = request.get_json(silent=True) or {}
    return _feature_call("verify", lambda i, m: engine.verify(i, m, b.get("claim", "")))


@app.route("/api/advise", methods=["POST"])
def api_advise():
    b = request.get_json(silent=True) or {}
    return _feature_call("advise", lambda i, m: engine.advise(i, m, b.get("position", "")))


@app.route("/api/zones", methods=["POST"])
def api_zones():
    return _feature_call("zones", engine.zones)


@app.route("/api/history")
def api_history():
    uid = current_uid()
    if uid is None:
        return jsonify({"error": "auth_required"}), 401
    return jsonify({"history": db.list_history(uid)})


# ------------------------------------------------------- live quant engine
@app.route("/api/markets")
def api_markets():
    import market
    return jsonify({"symbols": [
        {"symbol": k, "label": v["label"], "kind": v["kind"]}
        for k, v in market.SYMBOLS.items()],
        "timeframes": list(market.TIMEFRAMES)})


@app.route("/api/quant")
def api_quant():
    """A rules-based plan from live market data (no image, no key needed)."""
    import market
    if not security.allow(f"ip:{client_ip()}"):
        return jsonify({"error": "rate_limited"}), 429
    symbol = request.args.get("symbol", "XAUUSD")
    timeframe = request.args.get("timeframe", "H1")
    account = _num("account", 200.0)
    risk = _num("risk", 1.0)
    try:
        return jsonify(engine.quant_analyze(symbol, timeframe, account, risk))
    except market.MarketError as exc:
        return jsonify({"error": "market", "detail": str(exc)}), 502
    except ValueError as exc:
        return jsonify({"error": "bad_symbol", "detail": str(exc)}), 400


@app.route("/api/scan")
def api_scan():
    """Rank every instrument by the strength of its current setup."""
    import market
    if not security.allow(f"ip:{client_ip()}"):
        return jsonify({"error": "rate_limited"}), 429
    timeframe = request.args.get("timeframe", "H1")
    account = _num("account", 200.0)
    risk = _num("risk", 1.0)
    try:
        return jsonify(engine.quant_scan(timeframe, account, risk))
    except market.MarketError as exc:
        return jsonify({"error": "market", "detail": str(exc)}), 502


def _num(name: str, default: float) -> float:
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "too_large", "detail": "max 12 MB"}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Market analysis platform on http://localhost:{port}  "
          f"(provider={llm.provider_name()})")
    app.run(host="0.0.0.0", port=port, debug=False)
