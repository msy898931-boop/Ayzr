"""
SQLite persistence: users, plans, credit usage, signals and history.
====================================================================

Standard-library only (``sqlite3``). One small schema, created on first run.
Passwords are stored as salted PBKDF2 hashes -- never in plain text.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time

DB_PATH = os.environ.get("TRADLI_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "tradli.db")

PLANS = {
    "free": {
        "label_en": "Free", "label_ar": "مجاني", "price": 0, "days": 0,
        "limits": {"analyze": 1, "verify": 1, "advise": 1, "zones": 0},
        "note_en": "One analysis per section (lifetime)", "note_ar": "تحليل واحد لكل قسم (مرة واحدة)",
    },
    "basic": {
        "label_en": "Basic", "label_ar": "أساسي", "price": 39.99, "days": 30,
        "limits": {"analyze": 3, "verify": 3, "advise": 3, "zones": 2},
        "note_en": "Perfect for active traders", "note_ar": "مناسب للمتداول النشط",
    },
    "pro": {
        "label_en": "Pro", "label_ar": "احترافي", "price": 59.99, "days": 30,
        "limits": {"analyze": 10, "verify": 6, "advise": 6, "zones": 4},
        "note_en": "Most popular choice", "note_ar": "الأكثر اختياراً",
    },
    "ultimate": {
        "label_en": "Ultimate", "label_ar": "شامل", "price": 99, "days": 30,
        "limits": {"analyze": 15, "verify": 10, "advise": 10, "zones": 8},
        "note_en": "For professional traders", "note_ar": "للمتداول المحترف",
    },
}

FEATURE_LABEL = {
    "analyze": ("Chart Analysis", "تحليل الشارت"),
    "verify": ("Signal Verification", "التحقق من الإشارة"),
    "advise": ("Trading Advisor", "المستشار الذكي"),
    "zones": ("Supply & Demand Zones", "مناطق العرض والطلب"),
}


class _ClosingConnection(sqlite3.Connection):
    """A connection that closes when used as a context manager.

    ``sqlite3.Connection.__exit__`` commits (or rolls back) but does *not* close
    the handle, so `with connect() as con:` leaks a file descriptor on every
    call. Overriding `__exit__` to close makes every existing `with` block
    release the connection, without touching the call sites.
    """

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, factory=_ClosingConnection)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                pw_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT 'free',
                plan_expires REAL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER NOT NULL,
                feature TEXT NOT NULL,
                day TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, feature, day)
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                feature TEXT NOT NULL,
                symbol TEXT,
                timeframe TEXT,
                payload TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry REAL,
                stop REAL,
                targets TEXT,
                credibility INTEGER NOT NULL DEFAULT 50,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                amount REAL NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                provider_ref TEXT,
                provider_sub TEXT,
                kind TEXT NOT NULL DEFAULT 'initial',
                auto_renew INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created REAL NOT NULL,
                paid_at REAL
            );
            -- One payment activates exactly one order, no matter how many times
            -- the provider retries the webhook.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_ref
                ON orders (provider, provider_ref)
                WHERE provider_ref IS NOT NULL;
            """
        )
        _migrate(con)


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after the first release.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a database
    created before subscriptions existed lacks ``users.plan_expires``. SQLite has
    no ``ADD COLUMN IF NOT EXISTS``, so we check ``PRAGMA table_info`` first.
    """
    user_cols = {r["name"] for r in con.execute("PRAGMA table_info(users)").fetchall()}
    if "plan_expires" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN plan_expires REAL")
    order_cols = {r["name"] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
    if "provider_sub" not in order_cols:
        con.execute("ALTER TABLE orders ADD COLUMN provider_sub TEXT")
    if "kind" not in order_cols:
        con.execute("ALTER TABLE orders ADD COLUMN kind TEXT NOT NULL DEFAULT 'initial'")
    if "auto_renew" not in order_cols:
        con.execute("ALTER TABLE orders ADD COLUMN auto_renew INTEGER NOT NULL DEFAULT 0")


# ---------------------------------------------------------------- passwords
def hash_pw(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return h.hex(), salt


def verify_pw(password: str, pw_hash: str, salt: str) -> bool:
    h, _ = hash_pw(password, salt)
    return secrets.compare_digest(h, pw_hash)


# ------------------------------------------------------------------- users
def create_user(email: str, password: str) -> dict | None:
    email = (email or "").strip().lower()
    if not email or "@" not in email or len(password or "") < 6:
        return None
    pw_hash, salt = hash_pw(password)
    try:
        with connect() as con:
            cur = con.execute(
                "INSERT INTO users (email, pw_hash, salt, plan, created) VALUES (?,?,?,?,?)",
                (email, pw_hash, salt, "free", time.time()),
            )
            return {"id": cur.lastrowid, "email": email, "plan": "free"}
    except sqlite3.IntegrityError:
        return None


def get_user_by_email(email: str) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM users WHERE email=?",
                           ((email or "").strip().lower(),)).fetchone()


def get_user(uid: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def set_plan(uid: int, plan: str, days: int | None = None,
             now: float | None = None) -> bool:
    """Grant `plan` to a user.

    With `days` the plan is a time-boxed subscription: it starts now and lasts
    `days`. Without `days` it is permanent (used by tests and manual grants).
    A paid plan with no expiry is what an old order used to produce; we keep it
    working so existing rows do not silently lose access.
    """
    if plan not in PLANS:
        return False
    expires = None
    if days:
        expires = (now if now is not None else time.time()) + days * 86400
    with connect() as con:
        con.execute("UPDATE users SET plan=?, plan_expires=? WHERE id=?",
                    (plan, expires, uid))
    return True


def plan_active(uid: int, now: float | None = None) -> bool:
    """True when the user's paid plan is still within its term."""
    row = get_user(uid)
    if row is None or row["plan"] == "free":
        return False
    exp = row["plan_expires"] if "plan_expires" in row.keys() else None
    if exp is None:
        return True  # permanent grant
    return (now if now is not None else time.time()) < exp


def _plan_expired(user: sqlite3.Row | None, now: float | None = None) -> bool:
    """A paid plan past its expiry date behaves as free."""
    if user is None or user["plan"] == "free":
        return False
    exp = user["plan_expires"] if "plan_expires" in user.keys() else None
    return exp is not None and (now if now is not None else time.time()) >= exp


def effective_plan(user: sqlite3.Row | None, now: float | None = None) -> str:
    """The plan a user is *entitled to right now* -- 'free' once a term lapses."""
    if user is None:
        return "free"
    return "free" if _plan_expired(user, now) else user["plan"]


def days_left(user: sqlite3.Row | None, now: float | None = None) -> int | None:
    """Whole days remaining in the subscription, or None when there is no term.

    Rounds up, so the last partial day still shows as 1 rather than 0.
    """
    if user is None:
        return None
    exp = user["plan_expires"] if "plan_expires" in user.keys() else None
    if exp is None:
        return None
    remaining = exp - (now if now is not None else time.time())
    return max(0, int(-(-remaining // 86400)))


# ------------------------------------------------------------- credit meter
def _today() -> str:
    return time.strftime("%Y-%m-%d")


def usage_for(uid: int, feature: str) -> dict:
    with connect() as con:
        row = con.execute(
            "SELECT used, total FROM usage WHERE user_id=? AND feature=? AND day=?",
            (uid, feature, _today()),
        ).fetchone()
    return dict(row) if row else {"used": 0, "total": 0}


def plan_limits(plan: str) -> dict:
    return PLANS.get(plan, PLANS["free"])["limits"]


def quota(uid: int, feature: str) -> dict:
    """How many of `feature` this user may still use today."""
    user = get_user(uid)
    plan = effective_plan(user)
    limit = plan_limits(plan).get(feature, 0)
    used = usage_for(uid, feature)["used"]
    # free plan: the lifetime allowance lives in `total`
    if plan == "free":
        used = usage_for(uid, feature)["total"]
        limit = PLANS["free"]["limits"].get(feature, 0)
    remaining = max(0, limit - used)
    return {"plan": plan, "limit": limit, "used": used, "remaining": remaining}


def consume(uid: int, feature: str) -> bool:
    """Try to spend one credit. Returns False when the quota is exhausted.

    The spend is a single conditional UPDATE, so it is atomic: two concurrent
    requests can never both pass a "remaining > 0" read and over-spend.
    """
    user = get_user(uid)
    plan = effective_plan(user)
    limit = plan_limits(plan).get(feature, 0)
    if limit <= 0:
        return False
    col = "total" if plan == "free" else "used"
    with connect() as con:
        con.execute(
            """INSERT INTO usage (user_id, feature, day, used, total)
               VALUES (?,?,?,0,0)
               ON CONFLICT(user_id, feature, day) DO NOTHING""",
            (uid, feature, _today()),
        )
        cur = con.execute(
            f"UPDATE usage SET {col}={col}+1 "
            f"WHERE user_id=? AND feature=? AND day=? AND {col} < ?",
            (uid, feature, _today(), limit),
        )
        return cur.rowcount == 1


def refund(uid: int, feature: str) -> None:
    """Give back one credit after a failed attempt.

    A credit is only ever spent against a *successful* run, so a bad image or an
    engine error must not cost the user. Best-effort: never raise.
    """
    user = get_user(uid)
    plan = effective_plan(user)
    col = "total" if plan == "free" else "used"
    try:
        with connect() as con:
            con.execute(
                f"UPDATE usage SET {col}=MAX({col}-1,0) "
                f"WHERE user_id=? AND feature=? AND day=?",
                (uid, feature, _today()),
            )
    except sqlite3.Error:
        pass


# ----------------------------------------------------------------- history
def save_history(uid: int, feature: str, payload: dict,
                 symbol: str = "", timeframe: str = "") -> int:
    with connect() as con:
        cur = con.execute(
            """INSERT INTO history (user_id, feature, symbol, timeframe, payload, created)
               VALUES (?,?,?,?,?,?)""",
            (uid, feature, symbol, timeframe, json.dumps(payload, ensure_ascii=False),
             time.time()),
        )
        return cur.lastrowid


def list_history(uid: int, limit: int = 20) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


# ----------------------------------------------------------------- signals
def seed_signals() -> None:
    with connect() as con:
        n = con.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
        if n:
            return
        now = time.time()
        rows = [
            ("GoldRoom", "XAUUSD", "BUY", 2410.5, 2398.0, [2425, 2440, 2460], 74),
            ("FX_Signals", "EURUSD", "SELL", 1.0865, 1.0900, [1.0830, 1.0800, 1.0770], 61),
            ("CryptoEdge", "BTCUSD", "BUY", 61200, 59800, [62800, 64500, 66000], 58),
            ("PipsHunter", "GBPJPY", "SELL", 198.40, 199.20, [197.5, 196.6, 195.8], 43),
            ("AlphaDesk", "US30", "BUY", 39400, 39150, [39700, 40000, 40300], 69),
            ("QuickScalps", "USDJPY", "BUY", 157.20, 156.60, [157.8, 158.4, 159.0], 37),
        ]
        for i, (ch, sym, d, e, s, t, c) in enumerate(rows):
            con.execute(
                """INSERT INTO signals (channel, symbol, direction, entry, stop,
                                        targets, credibility, created)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (ch, sym, d, e, s, json.dumps(t), c, now - i * 900),
            )


def list_signals(max_age_min: int | None = None) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM signals ORDER BY created DESC").fetchall()
    out = []
    for r in rows:
        age_min = (time.time() - r["created"]) / 60
        if max_age_min is not None and age_min > max_age_min:
            continue
        out.append({**dict(r), "targets": json.loads(r["targets"] or "[]"),
                    "age_min": int(age_min)})
    return out


# The set of directions a signal may carry. Kept here (not in the route) so the
# validation rule lives beside the data it protects.
SIGNAL_DIRECTIONS = ("BUY", "SELL")


def add_signal(channel: str, symbol: str, direction: str, entry: float | None,
               stop: float | None, targets: list, credibility: int) -> dict | None:
    """Insert a signal from an authenticated ingest call.

    Every field is normalised and bounded here: the values are rendered in the
    UI, so an unbounded channel name is both a spam and an injection vector.
    Returns the stored row, or None when the payload is not acceptable.
    """
    channel = " ".join(str(channel or "").split())[:40]
    symbol = " ".join(str(symbol or "").split()).upper()[:16]
    direction = str(direction or "").upper()
    if not channel or not symbol or direction not in SIGNAL_DIRECTIONS:
        return None
    try:
        credibility = max(0, min(100, int(credibility)))
    except (TypeError, ValueError):
        return None
    tgts = []
    for t in (targets or [])[:5]:
        try:
            tgts.append(float(t))
        except (TypeError, ValueError):
            continue
    def _num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    now = time.time()
    with connect() as con:
        cur = con.execute(
            """INSERT INTO signals (channel, symbol, direction, entry, stop,
                                    targets, credibility, created)
               VALUES (?,?,?,?,?,?,?,?)""",
            (channel, symbol, direction, _num(entry), _num(stop),
             json.dumps(tgts), credibility, now),
        )
        row = con.execute("SELECT * FROM signals WHERE id=?",
                          (cur.lastrowid,)).fetchone()
    return {**dict(row), "targets": json.loads(row["targets"] or "[]"),
            "age_min": 0}


# ------------------------------------------------------------------ orders
def create_order(uid: int, plan: str, amount: float, provider: str = "",
                 auto_renew: bool = False, kind: str = "initial") -> int:
    with connect() as con:
        cur = con.execute(
            """INSERT INTO orders
               (user_id, plan, amount, provider, kind, auto_renew, status, created)
               VALUES (?,?,?,?,?,?, 'pending', ?)""",
            (uid, plan, float(amount), provider, kind, 1 if auto_renew else 0,
             time.time()),
        )
        return cur.lastrowid


def get_order(order_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def order_by_ref(provider: str, provider_ref: str) -> sqlite3.Row | None:
    """The order a given provider charge already settled, if any.

    Used to make a retried renewal webhook idempotent *before* we insert a new
    row: without this a provider retry would create a second renewal order that
    then collides with the unique index, reporting ``conflict`` instead of the
    truthful ``already``.
    """
    if not provider_ref:
        return None
    with connect() as con:
        return con.execute(
            "SELECT * FROM orders WHERE provider=? AND provider_ref=?",
            (provider, provider_ref)).fetchone()


def list_orders(uid: int, limit: int = 20) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def activate_order(order_id: int, provider: str, provider_ref: str,
                   provider_sub: str | None = None) -> dict:
    """Mark an order paid and grant its plan -- exactly once.

    Returns ``{"status": "activated"|"already"|"unknown"|"conflict", ...}``.
    The unique index on (provider, provider_ref) is what makes a repeated
    webhook delivery harmless: the second attempt collides and we report
    ``already`` instead of granting the plan twice.

    ``provider_sub`` records the provider-side subscription id (the thing a
    renewal charge will quote), so a later ``invoice.paid`` can be tied back to
    the user without trusting anything the caller says.
    """
    now = time.time()
    with connect() as con:
        row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            return {"status": "unknown"}
        if row["status"] == "paid":
            return {"status": "already", "plan": row["plan"], "user_id": row["user_id"]}
        try:
            con.execute(
                "UPDATE orders SET status='paid', paid_at=?, provider=?, provider_ref=?, "
                "provider_sub=COALESCE(?, provider_sub) WHERE id=?",
                (now, provider, provider_ref, provider_sub, order_id),
            )
        except sqlite3.IntegrityError:
            # Same provider_ref already settled a different order: refuse.
            return {"status": "conflict"}
        expires = _extend_term(con, row["user_id"], row["plan"], now)
    return {"status": "activated", "plan": row["plan"], "user_id": row["user_id"],
            "expires": expires}


def _extend_term(con: sqlite3.Connection, uid: int, plan: str,
                 now: float) -> float | None:
    """Grant `plan` to `uid` for its term, extending any remaining time.

    Used by both the first payment and every renewal, so both share one rule:
    a customer is never cut short. Renewing while a term is running adds `days`
    on top of the current expiry; once it has lapsed the term restarts from now.
    """
    user = con.execute("SELECT plan, plan_expires FROM users WHERE id=?",
                       (uid,)).fetchone()
    base = now
    cur_exp = user["plan_expires"] if user else None
    if cur_exp and user["plan"] == plan and cur_exp > now:
        base = cur_exp
    days = PLANS.get(plan, {}).get("days")
    expires = base + days * 86400 if days else None
    con.execute("UPDATE users SET plan=?, plan_expires=? WHERE id=?",
                (plan, expires, uid))
    return expires


# ------------------------------------------------------------- subscriptions
def active_subscription(uid: int) -> dict | None:
    """The paid order that backs the user's current term, if any.

    This is what a renewal charges against and what a cancel switches off. We
    pick the most recent *paid* order that carries a provider subscription id;
    an order without one (a one-off manual grant) has nothing to renew.
    """
    with connect() as con:
        row = con.execute(
            """SELECT * FROM orders
               WHERE user_id=? AND status='paid' AND provider_sub IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (uid,),
        ).fetchone()
    return dict(row) if row else None


def user_for_sub(provider_sub: str) -> sqlite3.Row | None:
    """Resolve a provider subscription id to its owner.

    A renewal webhook quotes only this id; the user and the plan come from our
    own records, never from the caller's payload.
    """
    if not provider_sub:
        return None
    with connect() as con:
        row = con.execute(
            """SELECT u.* FROM users u
               JOIN orders o ON o.user_id = u.id
               WHERE o.provider_sub=? AND o.status='paid'
               ORDER BY o.id DESC LIMIT 1""",
            (provider_sub,),
        ).fetchone()
    return row


def set_auto_renew(uid: int, on: bool, provider_sub: str | None = None) -> None:
    """Toggle automatic renewal on the user's subscription record.

    `provider_sub` narrows the change to one subscription when the user has had
    more than one; without it we update every paid order that has a sub id.
    """
    with connect() as con:
        if provider_sub:
            con.execute(
                "UPDATE orders SET auto_renew=? WHERE user_id=? AND provider_sub=?",
                (1 if on else 0, uid, provider_sub))
        else:
            con.execute(
                "UPDATE orders SET auto_renew=? WHERE user_id=? AND provider_sub IS NOT NULL",
                (1 if on else 0, uid))


def auto_renewing(uid: int) -> bool:
    sub = active_subscription(uid)
    return bool(sub and sub["auto_renew"])


# -------------------------------------------------------------------- stats
def stats() -> dict:
    """Aggregate counters for the admin view. Read-only, cheap, no PII beyond
    an email and a timestamp on the recent-orders list."""
    with connect() as con:
        users_total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        by_plan = {r["plan"]: r["n"] for r in con.execute(
            "SELECT plan, COUNT(*) n FROM users GROUP BY plan").fetchall()}
        # Subscriptions that have actually lapsed: paid plan, expiry in the past.
        expired = con.execute(
            "SELECT COUNT(*) n FROM users WHERE plan != 'free' "
            "AND plan_expires IS NOT NULL AND plan_expires <= ?",
            (time.time(),)).fetchone()["n"]
        orders_total = con.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
        paid = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) rev FROM orders WHERE status='paid'"
        ).fetchone()
        by_status = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM orders GROUP BY status").fetchall()}
        recent = [dict(r) for r in con.execute(
            """SELECT o.id, o.plan, o.amount, o.status, o.created, u.email
               FROM orders o LEFT JOIN users u ON u.id = o.user_id
               ORDER BY o.id DESC LIMIT 20""").fetchall()]
    return {
        "users": {"total": users_total, "by_plan": by_plan, "expired": expired},
        "orders": {"total": orders_total, "by_status": by_status,
                   "paid": paid["n"], "revenue": round(paid["rev"], 2)},
        "recent_orders": recent,
    }

