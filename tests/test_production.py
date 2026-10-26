"""Settings, auth, limits, headers, metrics, health, the stored alert queue and migrations."""
import hashlib
import importlib
import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from mktsurv import db, ops
from mktsurv.store import AlertStore

KEY = "mktsurv_" + "x" * 40
HASHED = {f"sha256:{hashlib.sha256(KEY.encode()).hexdigest()}": {"user": "asha", "role": "analyst"}}
EV = {"event_type": "ADD", "order_id": "O1", "trader_id": "T", "side": "BUY", "price": 10.0, "qty": 5, "timestamp_ms": 1}


def prod_env(**over):
    env = {"MKTSURV_ENV": "production", "MKTSURV_API_KEYS": json.dumps(HASHED), "MKTSURV_DATABASE_URL": "sqlite://"}
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def boot(monkeypatch, **env):
    for k in [k for k in os.environ if k.startswith("MKTSURV_")]:
        monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(f"MKTSURV_{k}", v)
    import mktsurv.api as api

    return importlib.reload(api)


def spoof_events():
    """The simulated session's order flow, submitted as if it came from a venue feed."""
    from mktsurv.simulate import generate_market_session

    s = generate_market_session(symbol="ACME", seed=42)
    return [{"event_type": e.event_type, "order_id": e.order_id, "trader_id": e.trader_id, "account_id": e.account_id,
             "ip_address": e.ip_address, "side": e.side, "price": e.price, "qty": e.qty, "timestamp_ms": e.timestamp_ms}
            for e in s.events[:5000]], s.session_end_ts_ms


def test_production_settings_accepted():
    s = ops.load_settings(prod_env())
    assert s.production and not s.demo and not s.ephemeral


@pytest.mark.parametrize("change, message", [
    ({"MKTSURV_API_KEYS": None}, "API keys"),
    ({"MKTSURV_DATABASE_URL": None}, "DATABASE_URL"),
    ({"MKTSURV_API_KEYS": json.dumps({KEY: {"user": "asha", "role": "analyst"}})}, "sha256"),
    ({"MKTSURV_API_KEYS": json.dumps({"sha256:" + "0" * 64: {"user": "x", "role": "root"}})}, "roles"),
    ({"MKTSURV_API_KEYS": "not json"}, "JSON"),
    ({"MKTSURV_CORS_ORIGINS": "*"}, "CORS"),
    ({"MKTSURV_ENV": "prod"}, "development or production"),
    ({"MKTSURV_DATABASE_URL": "mysql://x"}, "postgresql"),
    ({"MKTSURV_RATE_LIMIT": "0"}, "positive"),
])
def test_production_refuses_to_start(change, message):
    with pytest.raises(ops.ConfigError, match=message):
        ops.load_settings(prod_env(**change))


def test_no_public_bind_without_keys():
    with pytest.raises(ops.ConfigError, match="without API keys"):
        ops.load_settings({"MKTSURV_HOST": "0.0.0.0"})
    assert ops.load_settings({"MKTSURV_HOST": "0.0.0.0", "MKTSURV_API_KEYS": json.dumps(HASHED)}).host == "0.0.0.0"
    assert ops.load_settings({}).database_url == "sqlite://"


def test_serve_exits_with_a_readable_error():
    env = {k: v for k, v in os.environ.items() if not k.startswith("MKTSURV_")}
    r = subprocess.run([sys.executable, "-m", "mktsurv", "serve", "--host", "0.0.0.0"], env=env,
                       capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode != 0 and "without API keys" in r.stderr


def test_roles_decide_what_a_key_may_do(monkeypatch):
    keys = {"k-feed": {"user": "oms", "role": "feed"}, "k-view": {"user": "vic", "role": "viewer"},
            "k-an": {"user": "asha", "role": "analyst"}, "k-mon": {"user": "prom", "role": "monitor"}}
    c = TestClient(boot(monkeypatch, API_KEYS=json.dumps(keys)).app)
    surveil = {"events": [EV]}
    assert c.post("/surveil", json=surveil).status_code == 401
    assert c.post("/surveil", json=surveil, headers={"x-api-key": "wrong"}).status_code == 401
    assert c.post("/surveil", json=surveil, headers={"x-api-key": "k-feed"}).status_code == 200
    assert c.get("/alerts", headers={"x-api-key": "k-feed"}).status_code == 403
    assert c.post("/surveil", json=surveil, headers={"x-api-key": "k-view"}).status_code == 403
    assert c.get("/alerts", headers={"authorization": "Bearer k-view"}).status_code == 200
    assert c.post("/alerts/x/review", json={"status": "dismissed", "note": "ok"}, headers={"x-api-key": "k-view"}).status_code == 403
    assert c.get("/metrics", headers={"x-api-key": "k-an"}).status_code == 403
    m = c.get("/metrics", headers={"x-api-key": "k-mon"})
    assert m.status_code == 200 and 'http_requests_total{method="POST",route="/surveil",status="200"}' in m.text


def test_alerts_are_stored_reviewed_and_survive_a_restart(monkeypatch, tmp_path):
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'alerts.db'}"
    api = boot(monkeypatch, DATABASE_URL=url)
    c = TestClient(api.app)
    before = c.get("/alerts", params={"limit": 1000}).json()["count"]
    events, end = spoof_events()
    r = c.post("/surveil", json={"symbol": "ACME", "events": events, "session_end_ts_ms": end})
    assert r.status_code == 200
    body = r.json()
    raised = body["report"]["unified_alerts"]
    assert raised and len(body["stored_alert_ids"]) == len(raised) and body["run_id"]
    demo = c.post("/surveil", json={"symbol": "ACME"}).json()  # the simulated session isn't stored
    assert demo["run_id"] is None
    first = body["stored_alert_ids"][0]
    rev = c.post(f"/alerts/{first}/review", json={"status": "escalated", "note": "same pattern as last week"})
    assert rev.status_code == 200 and rev.json()["status"] == "escalated"
    assert c.post("/alerts/nope/review", json={"status": "escalated", "note": "xx"}).status_code == 404
    assert c.post(f"/alerts/{first}/review", json={"status": "closed", "note": "xx"}).status_code == 422
    metrics = c.get("/metrics").text
    assert "mktsurv_decisions_total" in metrics and 'decision="review_escalated"' in metrics

    api = boot(monkeypatch, DATABASE_URL=url)  # a restart: same database, new process state
    c = TestClient(api.app)
    assert c.get("/alerts", params={"limit": 1000}).json()["count"] == before + len(raised)
    got = c.get(f"/alerts/{first}").json()
    assert got["status"] == "escalated" and got["reviews"][0]["note"] == "same pattern as last week"
    assert got["reviews"][0]["reviewer"] == "demo" and got["alert"]["pattern"] == raised[0]["pattern"]
    assert c.get("/alerts", params={"status": "escalated"}).json()["count"] >= 1
    assert c.get("/alerts/unknown").status_code == 404


def test_rate_limit(monkeypatch):
    c = TestClient(boot(monkeypatch, RATE_LIMIT="3").app)
    codes = [c.get("/alerts").status_code for _ in range(5)]
    assert codes[:3] == [200] * 3 and codes[3:] == [429, 429]
    assert int(c.get("/alerts").headers["retry-after"]) >= 1
    assert c.get("/healthz").status_code == 200  # probes are never limited


def test_body_size_cap(monkeypatch):
    c = TestClient(boot(monkeypatch, MAX_BODY_BYTES="1000").app)
    assert c.post("/surveil", json={"events": [dict(EV, order_id=f"O{i}") for i in range(20)]}).status_code == 413

    def chunks():  # no content-length: the cap still holds
        for _ in range(20):
            yield b"a" * 100

    assert c.post("/surveil", content=chunks(), headers={"content-type": "application/json"}).status_code == 413
    assert c.post("/surveil", json={"events": [EV]}).status_code == 200


def test_headers_cors_and_request_ids(monkeypatch):
    c = TestClient(boot(monkeypatch, CORS_ORIGINS="https://console.bank.example").app)
    r = c.get("/alerts", headers={"x-request-id": "abc-123", "origin": "https://console.bank.example"})
    assert r.headers["x-request-id"] == "abc-123" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == ops.API_CSP and r.headers["cache-control"] == "no-store"
    assert r.headers["access-control-allow-origin"] == "https://console.bank.example"
    assert "access-control-allow-origin" not in c.get("/alerts", headers={"origin": "https://evil.example"}).headers
    assert "unsafe-inline" in c.get("/").headers["content-security-policy"]
    assert len(c.get("/healthz", headers={"x-request-id": "bad id"}).headers["x-request-id"]) == 32


def test_errors_do_not_leak(monkeypatch):
    api = boot(monkeypatch)

    @api.app.get("/boom")
    def boom():
        raise RuntimeError("secret detail: trader ACCT-9 at 10.1.2.3")

    c = TestClient(api.app)
    r = c.get("/boom")
    assert r.status_code == 500 and "ACCT-9" not in r.text and r.json()["request_id"] == r.headers["x-request-id"]
    bad = c.post("/surveil", json={"events": [dict(EV, side="SIDEWAYS", trader_id="SECRET-TRADER")]})
    assert bad.status_code == 422 and "SECRET-TRADER" not in bad.text and "SIDEWAYS" not in bad.text


def test_health_and_readiness(monkeypatch):
    api = boot(monkeypatch)
    c = TestClient(api.app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json()["checks"] == {"database": True}
    monkeypatch.setattr(api.store.db, "connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["database"] is False and "db down" not in r.text


def test_json_logs_carry_request_ids(capsys):
    ops.setup_logging("INFO")
    token = ops.request_id.set("rid-1")
    ops.log.info("hello", extra={"fields": {"n": 1}})
    ops.request_id.reset(token)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["msg"] == "hello" and line["request_id"] == "rid-1" and line["n"] == 1


def test_migrations_apply_once(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    eng = db.engine(url)
    assert db.migrate(eng) == ["001_init.sql"]
    assert db.migrate(eng) == []
    with eng.connect() as c:
        tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
    assert {"surveillance_runs", "alerts", "alert_reviews", "schema_migrations"} <= tables
    AlertStore(eng).record("u", "ACME", 1, [])
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("MKTSURV_")}, "MKTSURV_DATABASE_URL": url}
    out = subprocess.run([sys.executable, "-m", "mktsurv", "migrate"], env=env, capture_output=True, text=True,
                         timeout=60, check=False)
    assert out.returncode == 0 and "up to date" in out.stdout
