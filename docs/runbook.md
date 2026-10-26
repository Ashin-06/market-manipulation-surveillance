# Runbook

How to deploy, configure and operate the surveillance service. Commands assume the repository root.

## Deploy

```bash
docker build -t market-manipulation-surveillance:1.0.0 .
python -m mktsurv.ops keygen           # one API key per feed, analyst and monitor
docker run -d --name mktsurv -p 8015:8015 -v mktsurv-data:/data \
  -e MKTSURV_KEYS_FILE=/run/secrets/mktsurv-keys.json \
  market-manipulation-surveillance:1.0.0
curl -fsS http://127.0.0.1:8015/readyz
```

The image runs in production mode as a non-root user and keeps its SQLite database in the `/data`
volume. It refuses to start, and logs why, if keys or the database URL are missing. Terminate TLS
in front of it. For more than one replica use Postgres
(`MKTSURV_DATABASE_URL=postgresql://user:pass@host/db`) and a shared rate limiter at the ingress.

Without Docker: `pip install -r requirements.lock && pip install -e .`, then `mktsurv serve`.

## Configure

| Variable | Required in production | Meaning |
|---|---|---|
| `MKTSURV_ENV` | — | `development` (default) or `production` |
| `MKTSURV_API_KEYS` / `MKTSURV_KEYS_FILE` | yes | JSON `{"sha256:<hex>": {"user": "...", "role": "..."}}`. Roles: `feed` (submit order flow), `viewer` (read alerts, episodes, bench), `analyst` (submit, read, review), `monitor` (`/metrics`), `admin` (all) |
| `MKTSURV_DATABASE_URL` | yes | `sqlite:////data/mktsurv.db` or `postgresql://...` (development default: in memory) |
| `MKTSURV_HOST`, `MKTSURV_PORT` | — | default `127.0.0.1:8015`; a non-loopback address needs API keys |
| `MKTSURV_RATE_LIMIT` | — | requests per minute per client (default 120) |
| `MKTSURV_MAX_BODY_BYTES` | — | default 2,000,000 (a request also holds at most 5,000 events) |
| `MKTSURV_CORS_ORIGINS` | — | comma-separated origins allowed to call from a browser; none by default |
| `MKTSURV_LOG_LEVEL` | — | default `INFO` |
| `FORWARDED_ALLOW_IPS` | — | proxies whose `X-Forwarded-For` is trusted (uvicorn) |

## Watch

- `GET /healthz`: the process is up (container health check).
- `GET /readyz`: the database answers; 503 otherwise.
- `GET /metrics` (role `monitor`): `http_requests_total`, `http_request_duration_seconds`,
  `errors_total`, and `mktsurv_decisions_total{decision=...}` with one series per alert pattern
  (`SPOOFING`, `LAYERING`, `WASH_TRADING`, `MARKING_THE_CLOSE`, `FRONT_RUNNING`) and per review
  outcome (`review_escalated`, `review_dismissed`, `review_open`).
- Logs: one JSON object per line on stdout, each with the `request_id` also returned in
  `X-Request-ID`.

Suggested alerts: `/readyz` failing for 2 minutes; `errors_total{kind="unhandled"}` rising; p95
latency of `/surveil` above 10 s; a pattern's alert count far above its usual daily level (a
threshold problem or a real event: look before tuning); open alerts older than your review SLA
(`GET /alerts?status=open`).

## Rotate keys

API keys are the only secrets. Add the new hash to the key file and restart, hand the new key to
the client, then remove the old hash and restart again. To revoke a key at once, remove its hash
and restart.

## Back up and restore

SQLite (in the `/data` volume):

```bash
docker exec mktsurv python -c "import sqlite3; s=sqlite3.connect('/data/mktsurv.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
docker cp mktsurv:/data/backup.db ./mktsurv-$(date +%F).db
```

Restore: stop the container, put the backup at `/data/mktsurv.db` in the volume, start it, check
`/readyz` and that `GET /alerts` shows the expected latest alerts.

Postgres: `pg_dump` / `pg_restore`. Migrations run on start, or with `python -m mktsurv migrate`.

Alerts and reviews are surveillance records: keep backups for as long as your regulator requires
and restrict who can read them.

## Incidents

1. **Leaked API key.** Remove its hash and restart. Look up its user's runs
   (`surveillance_runs.requested_by`) and reviews (`alert_reviews.reviewer`) since the leak and
   re-check any dismissals.
2. **Alert flood after a change in market conditions.** Don't change thresholds in production
   under pressure. Check whether one pattern or one symbol drives it, review a sample by hand,
   and change rules through a reviewed release with the benchmark and real-data runs repeated.
3. **Database lost or corrupt.** Restore the latest backup; resubmit order flow received since
   then, if the feed can replay it, so its alerts are raised again.
4. **Suspected tampering with reviews.** Compare `alert_reviews` with the latest backups and the
   request logs (each review is a `POST /alerts/{id}/review` with a request ID and the caller).
