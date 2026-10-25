# Security Audit

Scope: the Aurora Markets web app (`app/`), the running production service, its
dependencies, and the git history. Method: manual review of every route and
input, static analysis (`bandit`), dependency scan (`pip-audit`), and live
black-box probes against the running server.

Date: 2026-10-25

## Summary

The codebase was already hardened to a high standard: parameterised SQL,
magic-byte image validation, HMAC-verified webhooks, constant-time token
comparisons, CSRF + Content-Type guards, a nonce-based CSP, PBKDF2 password
hashing, login lockout, and a strict secret-redaction logger. No SQL injection,
command injection, path traversal, SSRF, or authentication-bypass was found.

Three real issues were found and fixed, all reflected-input / abuse related.
No High or Critical finding is open.

## Fixed findings

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
