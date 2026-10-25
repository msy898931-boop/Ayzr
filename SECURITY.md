# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

- Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  (Security → Report a vulnerability), or
- email the contact in [`/.well-known/security.txt`](app/server.py)
  (set `CONTACT_EMAIL` to a real address in production).

Include what you did, what happened, and the impact. We aim to acknowledge
within a few days. Please give us a reasonable window to ship a fix before any
public disclosure.

## Scope

This is an advisory market-analysis web app. The realistic attack surface is:

- the HTTP API and session/auth flow,
- the billing webhook,
- the signal-ingest endpoint,
- anything that renders user-supplied text or images.

The app is **advisory only** — it never connects to a broker and never places a
trade, so there is no order path to attack.

## What is already in place

Do not report these as findings unless you can show they fail:

- **CSRF** — a per-session token is minted in a `<meta>` tag and echoed on every
  state-changing request; it rotates on login/logout. (`CSRF_PROTECT`)
- **JSON-only writes** — mutating endpoints reject non-`application/json`
  bodies with `415`, which closes the classic HTML-form CSRF bypass.
  (`REQUIRE_JSON_CONTENT_TYPE`)
- **Rate limiting + login lockout** — per-IP throttling and a failed-login
  lockout; a Redis backend is used when `REDIS_URL` is set, otherwise an
  in-process fallback (per-worker).
- **Security headers + CSP nonce** — `X-Frame-Options: DENY`, a strict
  `Content-Security-Policy` with a per-request nonce, `Referrer-Policy`,
  `X-Content-Type-Options`, and HSTS in production.
- **Safe logging** — secrets and credentials are redacted before they reach the
  logs.
- **Password policy** — a minimum length and a blocklist of reserved email
  domains for signup.
- **Production guard** — with `TRADLI_ENV=production` the app refuses to boot
  without a strong `SECRET_KEY`, so sessions can never ride on a random key.
- **Billing integrity** — no plan is granted without a verified, signed payment
  webhook.
- **Secret hygiene** — `.env`, `data/`, `*.db` and logs are gitignored; no real
  keys are committed.

## Handling secrets

- Keep provider keys and `SECRET_KEY` in the environment (or a secret manager),
  never in the repo.
- In production, always set a long random `SECRET_KEY`
  (`python -c "import secrets; print(secrets.token_hex(32))"`).
- Set `TRUST_PROXY=true` **only** behind a reverse proxy you control; otherwise
  a client can spoof its IP and bypass rate limits.
- Rotate any credential that has ever been pasted into a chat, issue or commit.

## Supported versions

This is a single-line project; only the latest `main` is supported with fixes.
