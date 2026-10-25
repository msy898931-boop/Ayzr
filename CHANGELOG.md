# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The current version is defined once, in `app/config.py` (`VERSION`).

## [Unreleased]

### Added
- Project documentation set: `SECURITY.md`, `CONTRIBUTING.md`, this changelog.
- `ruff` lint configuration (`ruff.toml`) and a lint step in CI.
- Dependabot configuration for pip and GitHub Actions.

## [1.9.0] - 2026-10-25

### Added
- **Structural stop.** The trade stop now sits just beyond the nearest support
  (long) or resistance (short), padded by a small ATR buffer, so it marks the
  level that would actually invalidate the idea. `stop_mode` reports which rule
  won (`structure`, `volatility`, or `risk`), and `structure_stop_pips` records
  the level distance that was given up when the risk budget capped it.
- **Protection system.** CSRF tokens (minted in a `<meta>` tag, rotated on
  login/logout), a JSON-only guard on writes (`415` otherwise), a password
  policy, per-IP rate limiting and login lockout (Redis backend with an
  in-process fallback), a strict CSP with a per-request nonce, and safe logging
  that redacts secrets.
- Android WebView wrapper for the platform.

### Changed
- The trade stop is risk-aware: never tighter than 1x ATR, never wider than the
  distance a 0.01-lot trade can take inside the requested risk %.
- The UI is split into one page per tool (desktop sidebar + mobile tab bar).

### Security
- No plan can be granted without a verified, signed payment webhook.
- The app refuses to boot with `TRADLI_ENV=production` and no `SECRET_KEY`.
- `.env`, `data/`, `*.db` and logs are gitignored; no keys are committed.

### Notes
- **Advisory only — the platform never connects to a broker and never places a
  trade.** It shows a plan for a human to act on.

## [1.7.0] - 2026-10-24

### Added
- Initial public cut: 12-agent analysis roster, bilingual (AR/EN, RTL) UI,
  vision chart analysis, signal fraud detection, position advisor, and
  supply/demand zones.
- Real OHLC quant layer (confluence scoring, plan, sizing, cost) that the model
  only narrates.
- Branding and legal pages, plus the ops scaffolding (Dockerfile,
  docker-compose, gunicorn config, launch scripts).

[Unreleased]: https://github.com/msy898931-boop/Ayzr/compare/v1.9.0...HEAD
[1.9.0]: https://github.com/msy898931-boop/Ayzr/compare/v1.7.0...v1.9.0
[1.7.0]: https://github.com/msy898931-boop/Ayzr/releases/tag/v1.7.0
