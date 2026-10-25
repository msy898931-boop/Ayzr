# Contributing

Thanks for taking a look. This is a small, opinionated codebase; the notes
below are what keep it honest and testable.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q          # 254 tests, no network needed
```

Run the app locally:

```bash
PORT=12001 python app/server.py     # http://localhost:12001
```

## Ground rules

- **Honest numbers only.** Never claim a user count, accuracy figure or any
  statistic the code cannot back up. Every analysis result is tagged `LIVE`
  (a real model answered) or `DEMO` (the deterministic fallback).
- **Advisory only — never auto-trades.** Do not add broker/MT5 integration or
  any code path that places an order. The platform shows a plan for a human to
  act on. See `AGENTS.md`.
- **The model narrates; the rules decide.** In the quant path the LLM only
  *explains* numbers computed in `app/quant.py`; it must never invent a price,
  stop or size. Keep it that way.
- **No secrets in the repo.** Keys live in the environment. `.env`, `data/`,
  `*.db` and logs are gitignored — keep it so.

## Tests

- `python -m pytest -q` runs the whole suite. `conftest.py` blanks every
  provider key, so tests never touch the network or a model — they always
  exercise the DEMO path. `market.fetch` is monkeypatched where live data is
  simulated.
- Add a test with every behaviour change. Bug fixes start with a failing test.
- Prefer real code paths over mocks; justify any mock in a comment.
- CI (`.github/workflows/tests.yml`) runs the suite on Python 3.11 and 3.13,
  plus a set of named guards (billing, money, subscriptions, spot).

## Style

- Keep it dependency-light: the runtime is Flask + requests + gunicorn only.
- Lint before pushing: `pip install ruff && ruff check .`
  (config in `ruff.toml`; CI enforces it).
- Match the surrounding style. Comments explain *why*, not *what*.
- Conventional commit subjects (`Add`, `Fix`, `Polish`, `Correct`) read well in
  the log.

## Pull requests

1. Branch from `main`, keep the change focused.
2. Make sure `python -m pytest -q` and `ruff check .` pass.
3. Describe the behaviour change and how you verified it.
4. Update `README.md`, `AGENTS.md` or `CHANGELOG.md` when the change is
   user-visible.

## Security

See [`SECURITY.md`](SECURITY.md) for how to report a vulnerability privately.
