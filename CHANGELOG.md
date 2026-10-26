# Changelog

All notable changes to this project. Versions follow [semantic versioning](https://semver.org/).

## 1.0.0 — 2026-10-26

### Added
- `pyproject.toml` with a `mktsurv` console command, and pinned `requirements.lock` /
  `requirements-dev.lock` (generated with `uv pip compile --universal`).
- Container image: slim Python 3.12 base, non-root user, health check on `/healthz`.
- CI on every push: ruff, bandit, pip-audit, pytest with a coverage report.
- Settings from `MKTSURV_*` environment variables, checked at startup. `MKTSURV_ENV=production`
  refuses to start without hashed API keys and `MKTSURV_DATABASE_URL`; nothing listens on a
  public address without keys.
- Per-client API keys with roles (`feed`, `viewer`, `analyst`, `monitor`, `admin`), stored as
  `sha256:<hex>`; `python -m mktsurv.ops keygen` makes new ones. Replaces the single
  `MKTSURV_API_KEY`.
- Per-client rate limit, request body cap (now also for chunked bodies), security headers,
  CORS only for listed origins, validation errors that don't echo input.
- Alert queue: alerts raised on submitted order flow are stored (SQLite by default, Postgres
  through `MKTSURV_DATABASE_URL`) with the run that raised them and who sent it.
  `GET /alerts`, `GET /alerts/{id}` and `POST /alerts/{id}/review` (escalate, dismiss, reopen,
  with a note and the reviewer's name). Numbered SQL migrations, `python -m mktsurv migrate`.
- JSON logs with request IDs; `/healthz`, `/readyz` and Prometheus `/metrics`
  (`mktsurv_decisions_total` counts alerts by pattern and reviews by outcome).
- `SECURITY.md`, `docs/threat-model.md`, `docs/runbook.md`, `docs/model-card.md`.

### Changed
- `/health` is replaced by `/healthz` (liveness) and `/readyz` (database check).
- `POST /surveil` returns `run_id` and `stored_alert_ids` (null for the built-in simulated
  session, which is not stored).
- Unknown episode IDs return a fixed "episode not found" instead of echoing the ID.

### Unchanged
- The order book, the detectors, their thresholds, and every simulator and real-data number in
  the README and `results/real_data.json`.
