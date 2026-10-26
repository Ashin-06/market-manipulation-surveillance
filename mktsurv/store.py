"""Surveillance runs, the alerts they raised and analyst reviews, kept in the database
(see db.py) so the alert queue survives restarts and every decision has a name on it."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

STATUSES = ("open", "escalated", "dismissed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AlertStore:
    def __init__(self, engine: Engine):
        self.db = engine
        self._lock = threading.Lock()

    def record(self, user: str, symbol: str, events: int, alerts: list[dict]) -> tuple[str, list[str]]:
        """Store one run and its alerts; returns the run ID and the stored alert IDs."""
        run_id, now = uuid.uuid4().hex, _now()
        ids = [uuid.uuid4().hex for _ in alerts]
        with self._lock, self.db.begin() as c:
            c.execute(text("INSERT INTO surveillance_runs (id, created_at, requested_by, symbol, events, alerts) "
                           "VALUES (:i, :t, :u, :s, :e, :a)"),
                      {"i": run_id, "t": now, "u": user, "s": symbol, "e": events, "a": len(alerts)})
            for aid, a in zip(ids, alerts):
                c.execute(text("INSERT INTO alerts (id, run_id, created_at, symbol, pattern, primary_trader, status, alert) "
                               "VALUES (:i, :r, :t, :s, :p, :tr, 'open', :a)"),
                          {"i": aid, "r": run_id, "t": now, "s": symbol, "p": a["pattern"],
                           "tr": a["primary_trader_id"], "a": json.dumps(a)})
        return run_id, ids

    @staticmethod
    def _row(r) -> dict:
        return {"id": r.id, "run_id": r.run_id, "created_at": r.created_at, "symbol": r.symbol,
                "status": r.status, "alert": json.loads(r.alert)}

    def list(self, status: str | None = None, symbol: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM alerts WHERE 1 = 1", {"n": limit}
        if status:
            sql, args["st"] = sql + " AND status = :st", status
        if symbol:
            sql, args["sy"] = sql + " AND symbol = :sy", symbol
        with self.db.connect() as c:
            return [self._row(r) for r in c.execute(text(sql + " ORDER BY created_at DESC, id LIMIT :n"), args)]

    def get(self, alert_id: str) -> dict | None:
        with self.db.connect() as c:
            r = c.execute(text("SELECT * FROM alerts WHERE id = :i"), {"i": alert_id}).first()
            if r is None:
                return None
            reviews = c.execute(text("SELECT reviewed_at, reviewer, status, note FROM alert_reviews "
                                     "WHERE alert_id = :i ORDER BY reviewed_at"), {"i": alert_id}).mappings().all()
        return {**self._row(r), "reviews": [dict(x) for x in reviews]}

    def review(self, alert_id: str, reviewer: str, status: str, note: str) -> dict | None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with self._lock, self.db.begin() as c:
            if c.execute(text("UPDATE alerts SET status = :s WHERE id = :i"), {"s": status, "i": alert_id}).rowcount == 0:
                return None
            c.execute(text("INSERT INTO alert_reviews (alert_id, reviewed_at, reviewer, status, note) VALUES (:i, :t, :r, :s, :n)"),
                      {"i": alert_id, "t": _now(), "r": reviewer, "s": status, "n": note})
        return self.get(alert_id)
