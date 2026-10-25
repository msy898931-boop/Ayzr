# Security Audit

Scope: the Aurora Markets web app (`app/`), the running production service, its
dependencies, and the git history. Method: manual review of every route and
input, static analysis (`bandit`), dependency scan (`pip-audit`), and live
black-box probes against the running server.

Date: 2026-10-25 (third pass)

## Summary

The codebase was already hardened to a high standard: parameterised SQL,
magic-byte image validation, HMAC-verified webhooks, constant-time token
comparisons, CSRF + Content-Type guards, a nonce-based CSP, PBKDF2 password
hashing, login lockout, and a strict secret-redaction logger. No SQL injection,
command injection, path traversal, SSRF, or authentication-bypass was found.

Eleven issues were found and fixed across three passes, all reflected-input,
output-escaping, log-forging or abuse related. No High or Critical finding is
open.

## Fixed findings (third pass)

### 8. Log injection / log forging — Low/Medium

Caller-controlled values were written to the audit log verbatim: the `<provider>`
path segment on `POST /api/webhook/<provider>`, and the `email` on a failed
login. A value containing a newline (``evil%0aadmin=granted``) forged a second,
fake line in the operator's log — enough to hide real events, poison a log-based
alert, or mislead an investigation (CWE-117). `security.redact()` now strips all
C0 control characters (and DEL) before any value reaches a log line, and every
log call in `server.py` passes its fields through `redact()`. Regression tests:
`test_redact_strips_log_forging_characters`,
`test_login_cannot_forge_a_log_line`.

### 9. Attack-surface enumeration on a public endpoint — Low

`GET /api/health` (unauthenticated) reported `providers` (which keys are
configured) and, inside `security`, whether the admin API, signal ingest,
billing and webhook were enabled, plus `secret_key_set` and `trust_proxy`. That
is free reconnaissance: it tells an attacker exactly which optional surfaces are
live and where to focus. The endpoint now returns only what the UI needs to
label DEMO vs LIVE (provider, model, live, agent count, env, version, brand) and
a defensive-posture summary that omits every "is this feature on?" flag.

### 10. Session-scoped API responses were cacheable — Low

`/api/me`, `/api/billing/history` and friends return the CSRF token, the user's
plan and their invoices, but no `Cache-Control` was set, so an intermediary or
shared cache could store them. All `/api/*` responses now carry
`Cache-Control: no-store`.

### 11. Rate-limit check was a read/modify race — Low

`security.allow()` did `count(...) >= limit` and then a separate `add(...)`. Two
requests arriving together could both read the same count and both pass, so a
parallel burst briefly exceeded the configured limit (and the login lockout
could be nudged past its threshold). The backends now expose a single atomic
`hit(key, window, limit)` (a locked check-and-record in memory, an
`INCR`-and-compare in Redis) and `allow()` is a one-liner over it. Regression
test: `test_rate_limit_is_atomic_under_concurrency`.

## Fixed findings (second pass)

### 4. Reflected XSS, second vector — Medium

The first-pass fix made `/api/quant` return a generic message for an unknown
symbol, but `market.fetch()` raised `MarketError(f"unknown timeframe: {timeframe}")`
with the caller's raw value, and `/api/quant` returns that `str(exc)` verbatim in
`detail`. A request with a *valid* symbol and a crafted timeframe
(`/api/quant?symbol=XAUUSD&timeframe=<img src=x onerror=...>`) still reflected
markup. `market.fetch` now raises fixed strings (`"unknown timeframe"`), so no
caller input reaches the message. Regression test:
`test_quant_bad_timeframe_does_not_echo_it`.

### 5. Unescaped model output in the UI — Medium

`renderAnalyze`, `renderQuant` and the scan table interpolated **model- and
provider-supplied** fields straight into `innerHTML`: `direction`, `trend`,
`confidence`, `entry`, `stop`, `targets` (analyze), `symbol`, `timeframe`,
`label`, `verdict`, `score`, `lots` (quant/scan). The `_merge()` overlay copies
any model key into the payload, so a chart, caption, or hostile provider reply
could carry markup into the page. Every one of these fields is now passed through
`esc()` at the sink.

### 6. User enumeration by login timing — Low/Medium

`api_login` skipped the PBKDF2 check entirely when the email was unknown, so a
missing account answered in microseconds while a wrong password spent ~120k
iterations. That is a reliable oracle for which emails are registered. Login now
verifies against a fixed `db.DUMMY_PW_HASH` on the unknown-email path, so both
outcomes cost the same. Regression test:
`test_login_hashes_even_when_the_email_is_unknown`.

### 7. `NaN`/`Infinity` in JSON — Low

`float("nan")`/`float("inf")` parse successfully, so `/api/quant` and `/api/scan`
accepted `account=nan`/`risk=inf` and propagated them into the plan maths. Flask
then serialised the non-finite results as bare `NaN`/`Infinity` tokens, which are
invalid JSON and break the client's `JSON.parse`. `_num()` now rejects
non-finite input and clamps to a sane range, and `db.add_signal` drops
non-finite entry/stop/targets the same way. Regression test:
`test_quant_rejects_non_finite_numbers`.

## Fixed findings (first pass)

### 1. Reflected XSS via error `detail` — Medium

`/api/quant` and `/api/scan` echoed the caller-supplied `symbol` / `timeframe`
back inside the `detail` field of the JSON error body, and the front end
interpolated `detail` straight into `innerHTML`:

```js
if(!r.ok){ setLive(`<p class="err">${R.detail||R.error||t("error")}</p>`); }
```

A crafted value (`/api/quant?symbol=<img src=x onerror=...>`) therefore ran as
markup in the page. Fixes, both sides:

* `app/templates/index.html` — every error `detail` is now passed through
  `esc()` before it reaches `innerHTML` (three call sites).
* `app/server.py` — `/api/quant` returns a fixed message instead of the raw
  symbol; `/api/scan` validates the timeframe against the known set before the
  loop, so a bogus value can no longer be echoed into any row.

Regression tests: `test_quant_does_not_echo_the_symbol`,
`test_scan_rejects_a_bad_timeframe_without_echoing_it`.

### 2. Unthrottled outbound amplifier — Medium

`GET /api/providers` had no rate limit and called `llm.probe_all()`, which
sends a real request to every configured model provider on every call. A loop
against it could burn provider quota and turn the app into an outbound request
amplifier.

* `app/server.py` — the route is now rate limited per IP.
* `app/llm.py` — `probe_all()` caches its result for 30 seconds, so bursts
  collapse into one round of probes.

Regression test: `test_providers_is_rate_limited`.

### 3. Spoofable client IP defeats rate limiting — Medium

`client_ip()` read the **leftmost** entry of `X-Forwarded-For`, which is the
part a client controls. With `TRUST_PROXY=true` (needed behind the platform's
TLS proxy) anyone could rotate that value and bypass every per-IP limit and the
login lockout.

* `app/server.py` — the address is now taken `TRUSTED_PROXY_HOPS` entries from
  the **right** (the entry the last trusted proxy appended), ignoring the
  client-supplied prefix.
* `app/config.py` — new `TRUSTED_PROXY_HOPS` setting (default 1).

Regression test: `test_client_ip_ignores_client_supplied_forwarded_entries`.

## Reviewed and considered safe

* **SQL** — all 40 queries are parameterised; the only f-strings interpolate
  hard-coded column names (`total`/`used`), never caller data.
* **Uploads** — images are accepted only if their magic bytes match
  jpeg/png/gif/webp; no image library decodes them, so no decoder CVE surface.
* **Webhooks** — HMAC over the raw body with a replay window; settle() names
  the plan from our own order record, never from the caller.
* **Secrets** — `.env` is git-ignored and untracked; the only secret-shaped
  strings in history are test fixtures.
* **Dependencies** — `pip-audit` reports no known vulnerabilities
  (Flask 3.1.3, Werkzeug 3.1.9, requests 2.34.2, urllib3 2.8.0).
* **Templates** — no `|safe`, no autoescape override; the front end escapes at
  every `innerHTML` sink for untrusted fields.
* **Session** — cookie is `Secure; HttpOnly; SameSite=Lax`; cleared on login
  and logout; CSRF token rotated on privilege change.
* **Headers** — CSP (nonce, no `unsafe-inline`), HSTS, `X-Frame-Options: DENY`,
  `nosniff`, `Referrer-Policy`, COOP/CORP, `Permissions-Policy`.
* **Path traversal** — live probes (`/static/../app/config.py`, `%2e%2e`,
  encoded slashes, `/.env`) all return 404; `/api/download` builds
  `os.path.join(DOWNLOAD_DIR, <fixed name>)`, so no caller segment reaches the
  filesystem path. Method tampering (`PUT`/`DELETE`/`PATCH` on a GET route) is
  refused (403) by the CSRF guard; `TRACE` is 405.
* **SSRF** — the only outbound URLs are operator-configured constants
  (`PAYMENT_CANCEL_URL`, provider base URLs); no request field selects a host.
* **ReDoS** — `llm.extract_json`'s fence regex is non-nested; a 200 KB hostile
  reply parses in ~0.1 s. Provider replies are small and the call is bounded by
  `TIMEOUT`.

`bandit`'s remaining notices are false positives: the `urlopen` target is an
operator-configured constant, the two SQL f-strings interpolate fixed column
names, `app.run(host="0.0.0.0")` is only the dev entry point (production uses
gunicorn), and a best-effort `except: pass` reads image dimensions.

## Operational notes

* Rate-limit counters are per-worker with the default in-process backend. Behind
  more than one worker set `REDIS_URL` so the limit is global.
* Set a real `CONTACT_EMAIL`, `SECRET_KEY`, and (if used) `ADMIN_TOKEN` /
  `SIGNAL_INGEST_TOKEN` before going live; the admin and ingest APIs fail closed
  when their token is unset.
