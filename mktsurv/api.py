"""
FastAPI REST API & Web Console Server for `mktsurv` (Port 8015).

Endpoints:
  - GET  /               -> Interactive Level-3 Surveillance Web Console (`web/console.html`)
  - GET  /healthz, /readyz, /metrics -> liveness, readiness, Prometheus (role monitor)
  - POST /surveil        -> Run L3 LOB + 4 manipulation detectors on custom or simulated events [surveil]
                            (alerts on submitted events are stored for review)
  - GET  /alerts         -> Stored alerts, newest first, by status / symbol [read]
  - GET  /alerts/{id}    -> One alert with its review history [read]
  - POST /alerts/{id}/review -> Escalate, dismiss or reopen an alert, with a note [review]
  - GET  /episodes       -> List all simulated episodes + detected alerts + VPIN series [read]
  - GET  /episodes/{id}  -> Detailed L3 event trace, lifecycles, fills, and alerts for an episode [read]
  - GET  /bench          -> Benchmark comparison (`mktsurv` vs Naive Regulatory OTR > 15) [read]

Callers send an API key (X-API-Key or Authorization: Bearer); its role decides the
permissions in brackets. Settings and roles: see ops.py and docs/runbook.md. Without keys
the server runs open, for local use only, and refuses to bind a non-loopback address.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import db, ops
from .bench import run_benchmark
from .detectors import run_surveillance
from .lob import OrderEvent
from .simulate import generate_market_session
from .store import AlertStore

settings = ops.load_settings()
ops.setup_logging(settings.log_level)
store = AlertStore(db.connect(settings.database_url))

app = FastAPI(
    title="mktsurv — Level-3 Market Manipulation Surveillance Engine",
    version="1.0.0",
    description=(
        "Level-3 Limit Order Book (LOB) Microstructure Surveillance Engine for "
        "detecting Spoofing, Multi-Level Layering, Collusive Wash-Trading Rings, "
        "Marking-the-Close, and VPIN Order-Flow Toxicity / Front-Running."
    ),
)

ops.install(app, settings, {"database": lambda: store.db.connect().close()})

WEB_CONSOLE_PATH = Path(__file__).resolve().parent.parent / "web" / "console.html"
# The book rescans its levels on every event and the wash detector is quadratic in fills,
# so a single request is capped well below what a real session would hold.
MAX_EVENTS = 5_000


class OrderEventPayload(BaseModel):
    event_type: Literal["ADD", "CANCEL", "MODIFY", "MARKET"]
    order_id: str = Field(..., min_length=1, max_length=64)
    trader_id: str = Field(..., min_length=1, max_length=64)
    account_id: str = Field("ACCT-DEFAULT", max_length=64)
    ip_address: str = Field("127.0.0.1", max_length=64)
    side: Literal["BUY", "SELL"]
    price: float = Field(0.0, ge=0, le=1e9, allow_inf_nan=False)
    qty: int = Field(100, ge=0, le=10**9)
    timestamp_ms: int = Field(..., ge=0)
    tag: Optional[str] = Field(None, max_length=64)


class SurveilRequest(BaseModel):
    symbol: str = Field("ACME", max_length=16)
    vpin_bucket_volume: int = Field(500, ge=10, le=10**9)
    session_end_ts_ms: Optional[int] = Field(None, ge=0)
    events: Optional[List[OrderEventPayload]] = Field(None, max_length=MAX_EVENTS)


@lru_cache(maxsize=1)
def _get_cached_session_state() -> Dict[str, Any]:
    scenario = generate_market_session(symbol="ACME", seed=42)
    lob, report = run_surveillance(
        scenario.events,
        symbol=scenario.symbol,
        vpin_bucket_volume=500,
        session_end_ts_ms=scenario.session_end_ts_ms,
    )
    return {
        "scenario": scenario,
        "lob": lob,
        "report": report,
    }


@app.get("/", response_class=HTMLResponse)
def get_console() -> HTMLResponse:
    if WEB_CONSOLE_PATH.exists():
        return HTMLResponse(content=WEB_CONSOLE_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(
        content="<html><body><h1>mktsurv Level-3 Surveillance Engine</h1></body></html>"
    )


class ReviewRequest(BaseModel):
    status: Literal["open", "escalated", "dismissed"]
    note: str = Field(..., min_length=2, max_length=2000)


@app.post("/surveil")
def post_surveil(req: SurveilRequest, ident: dict = Depends(ops.require("surveil"))) -> Dict[str, Any]:
    submitted = bool(req.events)
    if req.events is not None and len(req.events) > 0:
        order_events = [
            OrderEvent(
                event_type=e.event_type,
                order_id=e.order_id,
                trader_id=e.trader_id,
                account_id=e.account_id,
                ip_address=e.ip_address,
                side=e.side,
                price=e.price,
                qty=e.qty,
                timestamp_ms=e.timestamp_ms,
                tag=e.tag,
            )
            for e in req.events
        ]
        session_end = req.session_end_ts_ms
    else:
        scenario = generate_market_session(symbol=req.symbol, seed=42)
        order_events = scenario.events
        session_end = req.session_end_ts_ms or scenario.session_end_ts_ms

    try:
        lob, report = run_surveillance(
            order_events,
            symbol=req.symbol,
            vpin_bucket_volume=req.vpin_bucket_volume,
            session_end_ts_ms=session_end,
        )
    except ValueError as exc:  # malformed event stream (duplicate IDs, bad prices, ...)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    latest_snap = lob.snapshot(
        timestamp_ms=order_events[-1].timestamp_ms if order_events else 0
    )
    alerts = [a.to_dict() for a in report.unified_alerts]
    for a in alerts:
        ops.DECISIONS.labels(decision=a["pattern"]).inc()
    run_id = alert_ids = None
    if submitted:  # the built-in simulated session is a demo; only real order flow is kept
        run_id, alert_ids = store.record(ident["user"] or "demo", req.symbol, len(order_events), alerts)
    return {
        "run_id": run_id,
        "stored_alert_ids": alert_ids,
        "report": report.to_dict(),
        "book_snapshot": latest_snap.to_dict(),
        "snapshots": [s.to_dict() for s in lob.snapshots[-40:]],
    }


@app.get("/alerts", dependencies=[Depends(ops.require("read"))])
def list_alerts(status: Optional[Literal["open", "escalated", "dismissed"]] = None,
                symbol: Optional[str] = Query(None, max_length=16),
                limit: int = Query(100, ge=1, le=1000)) -> Dict[str, Any]:
    rows = store.list(status, symbol, limit)
    return {"count": len(rows), "alerts": rows}


@app.get("/alerts/{alert_id}", dependencies=[Depends(ops.require("read"))])
def get_alert(alert_id: str) -> Dict[str, Any]:
    row = store.get(alert_id[:64])
    if row is None:
        raise HTTPException(404, "alert not found")
    return row


@app.post("/alerts/{alert_id}/review")
def review_alert(alert_id: str, body: ReviewRequest, ident: dict = Depends(ops.require("review"))) -> Dict[str, Any]:
    row = store.review(alert_id[:64], ident["user"] or "demo", body.status, body.note)
    if row is None:
        raise HTTPException(404, "alert not found")
    ops.DECISIONS.labels(decision=f"review_{body.status}").inc()
    return row


@app.get("/episodes", dependencies=[Depends(ops.require("read"))])
def list_episodes() -> Dict[str, Any]:
    state = _get_cached_session_state()
    scenario = state["scenario"]
    lob = state["lob"]
    report = state["report"]

    episodes_out = []
    for ep in scenario.episodes:
        matching_alerts = [
            a.to_dict()
            for a in report.unified_alerts
            if (set(ep.traders) & set(a.involved_traders)) and a.pattern == ep.pattern
        ]
        naive_hits = [
            t for t in ep.traders if t in report.naive_baseline_flagged_traders
        ]
        episodes_out.append(
            {
                **ep.to_dict(),
                "detected_by_mktsurv": len(matching_alerts) > 0,
                "flagged_by_naive_otr": len(naive_hits) > 0,
                "alerts": matching_alerts,
            }
        )

    return {
        "symbol": scenario.symbol,
        "total_events": len(lob.events),
        "total_fills": len(lob.fills),
        "episodes": episodes_out,
        "unified_alerts": [a.to_dict() for a in report.unified_alerts],
        "vpin": report.vpin.to_dict(),
    }


@app.get("/episodes/{episode_id}", dependencies=[Depends(ops.require("read"))])
def get_episode_detail(episode_id: str) -> Dict[str, Any]:
    state = _get_cached_session_state()
    scenario = state["scenario"]
    lob = state["lob"]
    report = state["report"]

    target_ep = next(
        (ep for ep in scenario.episodes if ep.episode_id.upper() == episode_id.upper()),
        None,
    )
    if target_ep is None:
        raise HTTPException(status_code=404, detail="episode not found")

    pad_ms = 5_000
    w_start = max(0, target_ep.start_ts_ms - pad_ms)
    w_end = target_ep.end_ts_ms + pad_ms
    traders_set = set(target_ep.traders)

    ep_events = [
        e.to_dict()
        for e in lob.events
        if (w_start <= e.timestamp_ms <= w_end) or (e.trader_id in traders_set)
    ]
    ep_fills = [
        f.to_dict()
        for f in lob.fills
        if (w_start <= f.timestamp_ms <= w_end)
        or (f.buyer_trader_id in traders_set or f.seller_trader_id in traders_set)
    ]
    ep_lifecycles = [
        lc.to_dict()
        for lc in lob.lifecycles.values()
        if lc.trader_id in traders_set
    ]
    ep_alerts = [
        a.to_dict()
        for a in report.unified_alerts
        if set(target_ep.traders) & set(a.involved_traders)
    ]

    return {
        "episode": target_ep.to_dict(),
        "detected_by_mktsurv": len(ep_alerts) > 0,
        "flagged_by_naive_otr": any(
            t in report.naive_baseline_flagged_traders for t in target_ep.traders
        ),
        "alerts": ep_alerts,
        "events": ep_events[:100],
        "fills": ep_fills[:100],
        "lifecycles": ep_lifecycles[:100],
    }


@app.get("/bench", dependencies=[Depends(ops.require("read"))])
def get_bench() -> Dict[str, Any]:
    return _cached_bench()


@lru_cache(maxsize=1)
def _cached_bench() -> Dict[str, Any]:
    return run_benchmark(seed=42)
