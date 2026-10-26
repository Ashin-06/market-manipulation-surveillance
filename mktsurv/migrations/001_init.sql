-- One row per POST /surveil on submitted order flow: who sent it and what it raised.
CREATE TABLE surveillance_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    symbol TEXT NOT NULL,
    events BIGINT NOT NULL,
    alerts BIGINT NOT NULL
);

-- Alerts are kept for review; alert holds the full alert JSON as raised.
CREATE TABLE alerts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    pattern TEXT NOT NULL,
    primary_trader TEXT NOT NULL,
    status TEXT NOT NULL,
    alert TEXT NOT NULL
);
CREATE INDEX alerts_status ON alerts (status, created_at);

-- Every analyst decision on an alert, oldest first; alerts.status is the latest one.
CREATE TABLE alert_reviews (
    alert_id TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE INDEX alert_reviews_alert ON alert_reviews (alert_id);
