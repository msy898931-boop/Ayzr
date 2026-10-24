# AGENTS.md — Aurora Markets platform

## What this is
A from-scratch, honest AI market-analysis platform (brand is configurable via AI chart
analysis, signal fraud detection, position advisor, supply/demand zones —
BRAND_NAME). AI chart analysis, signal fraud detection, position advisor and
supply/demand zones through a 12-agent roster, bilingual (AR/EN, RTL) UI.

## Run
```
pip install -r requirements.txt
PORT=12001 python app/server.py        # http://localhost:12001
./run.sh                               # same, with dep install + health check
```

## Test
```
pip install -r requirements-dev.txt
python -m pytest -q                    # 215 tests
python -m pytest --cov=app --cov-report=term-missing
```
Tests blank every provider key in `conftest.py`, so the suite never makes a
network/model call and always exercises the DEMO path. `market.fetch` is
monkeypatched in the engine tests. Coverage is measured over `app/`.

## Deploy
```
gunicorn -c gunicorn.conf.py wsgi:app          # production WSGI entry point
docker compose up -d --build                   # image + volume for data/
```
`TRADLI_ENV=production` requires `SECRET_KEY` and **refuses to boot without
it** (`config.secret_key()` raises SystemExit). `wsgi.py` is the gunicorn entry.

## Live vs demo — IMPORTANT
- No LLM key  -> `llm.provider_name()` returns "demo"; every payload has
  `live=False` and the UI shows a DEMO badge. Never relabel demo output as live.
- With `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`, calls go live
  and `live=True`. Provider + model are reported in `/api/health`.
- Never invent marketing metrics (user counts, accuracy %). The reference site
  shows numbers like "78% accuracy" / "18,359 users"; we deliberately do not.

## Architecture
- `app/market.py`   — live OHLC from Yahoo's public chart endpoint + indicators
  (SMA/EMA/RSI/MACD/ATR/Bollinger/ADX/Stochastic/pivots), all stdlib-only.
  15 instruments in `SYMBOLS`, 6 timeframes in `TIMEFRAMES`; 1h candles are
  aggregated for H4. Results cached 60s. Requires outbound network. Metals
  (XAUUSD/XAGUSD) are additionally anchored to a live *spot* feed -- see the
  gotcha below.
- `app/quant.py`    — transparent rules: confluence score per side, ATR stop,
  3 targets, position sizing, cost estimate. `size_position` emits a warning
  when the broker minimum lot (0.01) forces more risk than the user asked.
- `app/agents.py`   — the 12 agents (single source of truth; used by prompts,
  demo engine and UI). 11 specialists + `chief`.
- `app/engine.py`   — analyze / verify / advise / zones (vision) AND
  `quant_analyze` / `quant_scan`. In the quant path the model only *explains*
  rule-computed numbers; it never supplies them.
- `app/demo.py`     — deterministic fallback keyed by image SHA-256. Same image
  -> same answer. Not a prediction.
- `app/imaging.py`  — stdlib-only image validation, dimensions, fingerprint.
  The declared mime is never trusted: `validate_image` requires the magic bytes
  to match a real format or raises, so a non-image never reaches the paid model.
- `app/llm.py`      — provider layer; `extract_json` tolerates prose/fences.
- `app/db.py`       — SQLite: users, plans, usage credits, history, signals,
  orders. `activate_order` grants a plan exactly once (unique index on
  provider+ref), which is what makes a replayed payment webhook harmless.
  `_extend_term` is the single place a term is granted, shared by first payment
  and renewal. `_migrate` adds columns to pre-existing databases (`CREATE TABLE
  IF NOT EXISTS` never alters a table, so a new column needs an explicit `ALTER`).
- `app/billing.py`  — provider-agnostic payments: `checkout_url`,
  `verify_signature` (HMAC-SHA256, constant-time, timestamp window), `settle`
  (first payment *and* renewal) and `cancel_subscription`. The invariant it
  protects: a plan is granted *only* by a verified payment. There is no path
  where a user names a plan and gets it free.
- `app/config.py`   — every env-derived setting in one place. `secret_key()`
  refuses to boot in production without SECRET_KEY.
- `app/security.py` — abuse protection: per-IP rate limiting + login throttling
  behind a two-method interface (`allow` / `record_failure` / `locked`). The
  default backend is in-process; set `REDIS_URL` to move the counters to Redis
  and get one global limit across workers and hosts. A Redis outage falls back
  to memory rather than failing closed. `backend_name()` is reported in
  `/api/health`.
- `app/server.py`   — Flask routes; `_feature_call` centralises auth+quota+log.
  Sets a per-response CSP nonce and emits the security headers.

## Gotchas learned
- `market.snapshot()` levels: support must be BELOW price, resistance ABOVE.
  Do not just sort pivots; filter by side first or the UI lies.
- Confidence = score / sum(max points) of the *chosen* side. Using the buy
  side's denominators for a sell call produced >100% numbers once.
- Yahoo returns `close=None` for the forming candle; skip incomplete bars.
- Yahoo's XAUUSD proxy is the GC=F *futures* contract, which trades a few
  dollars away from the spot price a trader sees on MT5. `market.spot()` pulls
  the real spot and `_shift_to_spot` translates the whole series by one offset
  so the last close equals it. The shift preserves every candle's shape, so the
  indicators are identical before and after -- that is the whole point, and
  `test_spot.py::test_shift_leaves_indicators_unchanged` pins it. The spot feed
  is best-effort: a failure falls back to the Yahoo series (wrong basis, still
  real data) and never takes the read down.
- The raw candle series is cached, and the spot is applied *on return* from
  `fetch`. Do not bake the shifted series into the cache or a stale offset will
  stick for the whole TTL; `_shift_to_spot` also returns a copy for this reason.
- Indicators may be `None` (not enough history, e.g. EMA200 on short ranges).
  Always format defensively; never `.2f` a possibly-None value.
- Gemini retires model names without warning (`gemini-2.5-flash` -> 404 for new
  keys) and returns 503 under load. `llm.complete()` walks `GEMINI_MODELS` and
  then other configured providers, retrying on 404/429/5xx, so a single dead
  model never takes the app down. Keep that list current.
- Gemini "thinking" models spend output tokens on hidden reasoning; with a
  small `maxOutputTokens` the visible answer comes back empty (a candidate with
  no `parts`). We set `thinkingConfig.thinkingBudget=0` for 2.5/3.x models.
- Groq's catalogue rotates and most entries are text-only: sending an image to
  them fails with 400 "content must be a string". The vision-capable model is
  `qwen/qwen3.8-27b`; keep `GROQ_MODELS` to vision models only.
- Groq's free tier caps *output* tokens per minute (currently 1000) and rejects
  the entire request when `max_tokens` exceeds it. `MAX_OUT`/`_clamp()` lower
  the ask per provider; without it a 1400-token analysis 429s.
- Truncated JSON: a model that hits the output cap mid-object still returns a
  usable prefix. `llm.extract_json` -> `_salvage` closes dangling strings and
  brackets so a cut-off analysis degrades instead of falling back to DEMO.
- `GET /api/providers` probes each configured key (first error reported, not
  the last); `GET /api/download` serves `dist/Market-AI-Platform.zip`.
- API keys live in `.env` (gitignored, chmod 600). `server._load_dotenv` reads
  it with `setdefault`, so a real environment variable still wins.
- `base64.b64decode(validate=False)` *discards* non-alphabet characters instead
  of failing, so `"NOT-VALID!!!"` decoded to garbage bytes that passed as an
  "image" and were forwarded to the paid model. Use `validate=True` (with one
  padding retry) and then check the magic bytes.
- `sqlite3.Connection` as a context manager commits but does NOT close the
  handle. `with connect() as con:` silently leaked a file descriptor per call.
  `db._ClosingConnection` overrides `__exit__` to close, so every existing
  `with` block releases the connection. Run the suite with
  `-W error::ResourceWarning` to keep this from creeping back.
- A payment webhook must be authenticated by an HMAC over the *raw* body, not
  by anything in the URL or JSON fields. We parse the raw bytes ourselves
  (providers sometimes omit the JSON content type) and compare with
  `hmac.compare_digest`, with a timestamp window to stop replay.
- `.env` was once written with a typo'd key (missing char -> 401). When a key
  fails, compare length + bytes against the source before blaming the provider.
- Everything the model, a signal feed or a user can influence is rendered with
  `esc()` before it reaches `innerHTML`; model output is untrusted input. The
  page carries a per-response CSP nonce (`{{ csp_nonce }}`) and no
  `unsafe-inline`, so a single unescaped interpolation is also a script
  injection. Add a sink -> escape it; there is no inline `onclick` left on
  purpose (event delegation via `data-action`).
- The product name is `config.BRAND_NAME` everywhere it is shown, including the
  system prompts (`engine._brand`). A white-label deployment must not leak
  another brand into its own model prompts or its own UI.
- `POST /api/signals/ingest` is off without `SIGNAL_INGEST_TOKEN` (503). The
  signals hub is rendered into the page, so an open write endpoint would let a
  stranger put markup on every visitor's screen; `db.add_signal` also bounds
  every field length.

## Conventions
- Quotas: free plan counts lifetime uses (`total` column); paid plans count
  daily (`used` column keyed by date).
- A plan is a *subscription*: `users.plan_expires` holds the term end. Never
  read `users.plan` to decide entitlement -- call `db.effective_plan(user)`,
  which returns `free` once the term has lapsed. `quota`, `consume` and `refund`
  all go through it for exactly this reason.
- `plan_expires IS NULL` on a paid plan means a permanent grant (legacy rows and
  test helpers). Do not "clean those up" without migrating them to a term, or
  existing customers lose access.
- Renewal extends: `activate_order` adds `days` to the *current* expiry when the
  same plan is still active, and restarts from now when it has lapsed. Buying a
  different plan starts a fresh term rather than stacking.
- Recurring billing has two webhook shapes, both HMAC-verified: a first payment
  carries `order_id`; a renewal carries only `provider_sub` + `provider_ref`.
  Never grant on a renewal without resolving `provider_sub` to a user via
  `db.user_for_sub` -- the plan must come from our records, not the payload.
- A renewal is idempotent by `provider_ref`: `billing._settle_renewal` checks
  `db.order_by_ref` *before* inserting, so a provider retry returns `already`
  instead of creating a second renewal order (which would collide on the unique
  index and report `conflict`).
- Cancel stops future charges, never access. `auto_renew=0` is the guarantee;
  `billing.provider_cancel` is best-effort and must never raise. A charge that
  lands after a cancel is still honoured -- we never take money and deny service.
- `db.auto_renewing` / `active_subscription` read the newest *paid* order that
  has a `provider_sub`; orders without one are one-off grants with nothing to
  renew.
- A credit is spent *only* against a successful run. `server._feature_call`
  validates the image, then runs, and calls `db.refund` on any failure. If you
  add a feature, keep that order or users pay for errors.
- `db.consume` is a single conditional `UPDATE` on purpose. Do not "simplify" it
  back into SELECT-then-UPDATE: that reintroduces a race where N concurrent
  requests over-spend the quota (measured: 6 wins against a limit of 3).
- `db.refund` and `db.consume` must agree on the column (`total` for free,
  `used` for paid). Changing the quota model means touching both.
- The admin API is read-only and fails closed: no `ADMIN_TOKEN` -> 503, wrong
  token -> 403 (constant-time compare, rate limited). Never add a write route
  there without its own auth story.
- Passwords: PBKDF2-SHA256, 120k iterations, per-user salt. Never plaintext.
- Images: JSON `data:` URL or multipart `image`; max 8 MB decode / 12 MB request.
- UI strings live in the inline `I18N` dict in `index.html`; keep AR and EN in
  sync when adding keys.

## Testing
The suite lives in `tests/` (pytest). Run it with `python -m pytest -q`.
- `test_billing.py` is the one to keep honest: it pins the "no free upgrade"
  rule (a signed-in user POSTing `/api/plan` gets 403), that a webhook without a
  valid signature is rejected, and that replaying a payment is idempotent.
- `test_api.py::test_consume_is_atomic_under_concurrency` races threads against
  the quota; it fails if `consume` is ever written non-atomically.
- `test_subscriptions.py` pins the term logic: a lapsed plan must fall back to
  the free *lifetime* allowance (not the paid daily one), and renewal must
  extend rather than reset. Both are easy to get subtly wrong.
- `test_recurring.py` pins the recurring-billing money rules: a renewal extends
  the term and adds an order, a replayed charge never extends twice, a renewal
  for an unknown subscription is refused, and a charge that lands after a cancel
  is still honoured. Change `settle`/`cancel_subscription` and this must pass.
- `test_spot.py` pins the metals spot anchoring, including that the shift leaves
  every indicator unchanged and that a dead spot feed degrades to the Yahoo
  basis rather than raising.
- `test_billing_history.py` pins that `/api/billing/history` is scoped to the
  caller (one user must never see another's orders) and that the page's numbers
  agree with `/api/me`.
- `test_admin.py` pins that the admin API fails closed and that revenue counts
  only `paid` orders.
- `test_security_headers.py` pins the XSS contract: a hostile channel/plan name
  must come back escaped, the CSP header must carry a nonce and no
  `unsafe-inline`, and the product name must be configurable (no hardcoded
  brand in the rendered HTML).
- `test_rate_limit_backend.py` pins that the Redis backend enforces the same
  contract as memory, and that an unreachable Redis falls back to memory rather
  than failing closed.
- `test_signals_ingest.py` pins that ingest is off by default, needs the bearer
  token, and bounds every field (`db.add_signal`).
- Never let a test grant a plan by calling an endpoint. Grant it in the DB
  (`db.set_plan`) as the billing layer would, so the invariant stays testable.
- `conftest.py` blanks every provider key *before* importing the app, so tests
  always exercise the deterministic DEMO path and never make a real API call.
- The API was also verified end-to-end with a scripted client (register ->
  analyze -> quota exhaustion -> checkout -> signed webhook -> plan granted ->
  verify -> advise -> zones -> history). Re-run after changing quotas, auth,
  billing or the engine.
