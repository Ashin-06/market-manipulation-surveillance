"""The detectors on real Nasdaq order flow (LOBSTER sample files).

LOBSTER's free samples (lobsterdata.com) hold every add, cancel and execution inside the
top 10 price levels of AMZN, AAPL, GOOG, INTC and MSFT on 21 June 2012, 9:30 to 16:00, plus
the 10-level book after each message. There are no manipulation labels, so this measures
false alerts on (presumably) genuine traffic, not recall.

Nasdaq already matched these orders, so the order lifecycles and fills are built straight
from the messages instead of being re-matched by `lob.py`. Book imbalance at placement comes
from LOBSTER's book file (top 5 levels, as `lob.py` computes it).

The data carries no trader, account or IP fields. Every order is given to one of N
pseudo-traders uniformly at random, each with its own account and IP. Executions show only
the resting side, so each burst of executions with the same timestamp and side becomes one
aggressor order from another random pseudo-trader (never the owner of an order it hits: real
venues and brokers prevent self-matching; a burst that hits orders of every pseudo-trader is
split between aggressors). Hidden executions become fills against a random
pseudo-trader's hidden order. Under this attribution any alert is a coincidence of real
order flow, so every alert counts as false. Real ownership would cluster each firm's quoting
and could give more or fewer alerts.

The detectors run on 15-minute windows (the simulator's session length); marking the close
runs on the last window, ending at the 16:00 close. Orders placed before 9:30 or outside the
top 10 levels appear only when they are cancelled or executed; they are treated as resting
since 9:30.

    python -m mktsurv realdata --data DIR [--traders 10 50 200]
"""
from __future__ import annotations

import json
import random
from glob import glob
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

from .detectors import detect_marking_the_close, detect_spoofing_layering, detect_wash_trading, naive_otr_baseline
from .lob import OrderLifecycle, TradeFill
from .vpin import detect_front_running

OPEN_MS, CLOSE_MS, WINDOW_MS = 34_200_000, 57_600_000, 900_000
SIDE = {1: "BUY", -1: "SELL"}


def load(message_csv: str | Path, orderbook_csv: str | Path) -> pd.DataFrame:
    """LOBSTER messages plus the top-5 book imbalance after each one."""
    msgs = pd.read_csv(message_csv, header=None, names=["time", "type", "order_id", "size", "price", "direction"])
    sizes = pd.read_csv(orderbook_csv, header=None, usecols=list(range(1, 20, 2))).to_numpy(dtype=float)
    ask, bid = sizes[:, 0::2].sum(axis=1), sizes[:, 1::2].sum(axis=1)  # columns: ask px, ask sz, bid px, bid sz, ...
    if len(sizes) != len(msgs):
        raise ValueError("message and orderbook files differ in length")
    msgs["obi"] = np.divide(bid - ask, bid + ask, out=np.zeros(len(msgs)), where=(bid + ask) > 0)
    msgs["ts"] = np.round(msgs["time"].to_numpy() * 1000).astype(np.int64)
    return msgs


def build(msgs: pd.DataFrame, n_traders: int, seed: int = 0) -> SimpleNamespace:
    """Order lifecycles and fills from LOBSTER messages, with random pseudo-trader attribution."""
    rng = random.Random(seed)
    traders = [f"T{i:03d}" for i in range(n_traders)]
    ident = {t: (f"ACCT-{t}", f"10.{i // 250}.{i % 250}.1") for i, t in enumerate(traders)}
    lcs: Dict[str, OrderLifecycle] = {}
    fills: List[TradeFill] = []
    stats = {"messages": len(msgs), "unknown_order_refs": 0, "hidden_executions": 0, "halts_or_crosses": 0,
             "split_sweeps": 0}

    def order(oid: str, side: str, price: float, qty: int, ts: int, obi: float) -> OrderLifecycle:
        t = rng.choice(traders)
        lc = OrderLifecycle(oid, t, *ident[t], side, price, qty, remaining_qty=qty, placed_ts_ms=ts,
                            last_event_ts_ms=ts, book_imbalance_at_placement=obi)
        lcs[oid] = lc
        return lc

    def fill(ts: int, price: float, qty: int, aggr: OrderLifecycle, passive: OrderLifecycle) -> None:
        a, p = (aggr.trader_id, aggr.account_id, aggr.ip_address), (passive.trader_id, passive.account_id, passive.ip_address)
        buyer, seller = (a, p) if aggr.side == "BUY" else (p, a)
        fills.append(TradeFill(f"FILL-{len(fills) + 1:07d}", ts, price, qty, aggr.side, aggr.order_id, *a,
                               passive.order_id, *p, *buyer, *seller))

    rows = msgs[["ts", "type", "order_id", "size", "price", "direction", "obi"]].itertuples(index=False)
    pending: List[tuple] = []  # executions sharing a timestamp and side: one aggressor sweeps them

    def flush() -> None:
        while pending:
            # A burst that hits orders of every pseudo-trader is split between aggressors,
            # so none of them trades with itself
            owners: set = set()
            k = 0
            while k < len(pending) and len(owners | {pending[k][2].trader_id}) < n_traders:
                owners.add(pending[k][2].trader_id)
                k += 1
            k = max(k, 1)  # a lone pseudo-trader can only trade with itself
            burst = pending[:k]
            del pending[:k]
            stats["split_sweeps"] += bool(pending)
            ts, side = burst[0][0], burst[0][1]
            owners = {p.trader_id for _, _, p, _, _ in burst}
            t = rng.choice([x for x in traders if x not in owners] or traders)
            aggr = OrderLifecycle(f"AGG-{len(lcs)}", t, *ident[t], side, burst[0][3], sum(q for *_, q in burst),
                                  placed_ts_ms=ts, last_event_ts_ms=ts, first_fill_ts_ms=ts, status="FILLED")
            aggr.filled_qty = aggr.initial_qty
            lcs[aggr.order_id] = aggr
            for ts_, _, passive, price, qty in burst:
                fill(ts_, price, qty, aggr, passive)

    for ts, typ, oid, size, price, direction, obi in rows:
        px, oid, side = price / 10_000, str(oid), SIDE.get(direction, "BUY")
        if pending and (typ not in (4, 5) or ts != pending[0][0] or SIDE[-direction] != pending[0][1]):
            flush()
        if typ == 1:
            order(oid, side, px, int(size), ts, float(obi))
        elif typ in (2, 3, 4):
            lc = lcs.get(oid)
            if lc is None:  # placed before 9:30 or outside the top 10 levels
                stats["unknown_order_refs"] += 1
                lc = order(oid, side, px, int(size), OPEN_MS, 0.0)
            qty = min(int(size), lc.remaining_qty) if typ != 3 else lc.remaining_qty
            lc.remaining_qty -= qty
            lc.last_event_ts_ms = ts
            if typ == 4:
                lc.filled_qty += qty
                lc.first_fill_ts_ms = lc.first_fill_ts_ms or ts
                pending.append((ts, SIDE[-direction], lc, px, qty))
            else:
                lc.canceled_qty += qty
                lc.canceled_ts_ms = ts
            if lc.remaining_qty == 0:
                lc.status = "FILLED" if typ == 4 else ("CANCELED" if lc.filled_qty == 0 else "PARTIALLY_FILLED")
            elif lc.filled_qty:
                lc.status = "PARTIALLY_FILLED"
        elif typ == 5:
            stats["hidden_executions"] += 1
            hidden = order(f"HID-{len(lcs)}", side, px, int(size), ts, float(obi))
            hidden.filled_qty, hidden.remaining_qty, hidden.status = int(size), 0, "FILLED"
            pending.append((ts, SIDE[-direction], hidden, px, int(size)))
        else:
            stats["halts_or_crosses"] += 1
    flush()
    stats.update(orders=sum(not k.startswith(("AGG-", "HID-")) for k in lcs), fills=len(fills),
                 traded_shares=sum(f.qty for f in fills))
    return SimpleNamespace(lifecycles=lcs, fills=fills, stats=stats)


def surveil_windows(day: SimpleNamespace) -> dict:
    """Run every detector on each 15-minute window and count alerts and flagged traders."""
    lcs_by_w: Dict[int, dict] = {}
    for oid, lc in day.lifecycles.items():
        lcs_by_w.setdefault((lc.placed_ts_ms - OPEN_MS) // WINDOW_MS, {})[oid] = lc
    fills_by_w: Dict[int, list] = {}
    for f in day.fills:
        fills_by_w.setdefault((f.timestamp_ms - OPEN_MS) // WINDOW_MS, []).append(f)

    n_win = (CLOSE_MS - OPEN_MS) // WINDOW_MS
    alerts = {"SPOOFING": 0, "LAYERING": 0, "WASH_TRADING": 0, "MARKING_THE_CLOSE": 0, "FRONT_RUNNING": 0}
    wash_types: Dict[str, int] = {}
    flagged_tw = otr_tw = 0
    examples: List[dict] = []
    for w in range(n_win):
        view = SimpleNamespace(lifecycles=lcs_by_w.get(w, {}), fills=fills_by_w.get(w, []))
        spoof = detect_spoofing_layering(view)
        wash = detect_wash_trading(view.fills)
        close = detect_marking_the_close(view.fills, session_end_ts_ms=CLOSE_MS) if w == n_win - 1 else []
        front = detect_front_running(view.fills)
        for a in spoof:
            alerts[a.detector] += 1
        for a in wash:
            wash_types[a.detector] = wash_types.get(a.detector, 0) + 1
        alerts["WASH_TRADING"] += len(wash)
        alerts["MARKING_THE_CLOSE"] += len(close)
        alerts["FRONT_RUNNING"] += len(front)
        flagged = {a.trader_id for a in spoof + close + front} | {t for a in wash for t in a.traders}
        flagged_tw += len(flagged)
        otr_tw += len(naive_otr_baseline(view))
        for a in (spoof + wash + close + front)[:2]:
            if len(examples) < 12:
                d = a.to_dict()
                examples.append({k: v for k, v in d.items() if not k.endswith(("_ids", "fill_ids"))})
    return {"alerts": alerts, "wash_by_type": wash_types, "flagged_trader_windows": flagged_tw,
            "otr_flagged_trader_windows": otr_tw, "windows": n_win, "examples": examples}


def experiments(data_dir: str | Path, traders: Sequence[int] = (10, 50, 200), seeds: Sequence[int] = (0, 1, 2),
                log=print) -> dict:
    """Alert counts per stock and pseudo-trader count, averaged over random attributions (seeds)."""
    hours = (CLOSE_MS - OPEN_MS) / 3_600_000
    res: dict = {"dataset": "LOBSTER sample files, Nasdaq, 21 June 2012, 10 levels", "hours_per_day": hours,
                 "attribution": "orders and aggressors assigned to pseudo-traders uniformly at random",
                 "seeds": list(seeds), "stocks": {}}
    mean = lambda xs: round(sum(xs) / len(xs), 2)
    for msg_csv in sorted(glob(str(Path(data_dir) / "*_message_10.csv"))):
        ticker = Path(msg_csv).name.split("_")[0]
        msgs = load(msg_csv, msg_csv.replace("_message_", "_orderbook_"))
        res["stocks"][ticker] = out = {}
        for n in traders:
            runs = []
            for s in seeds:
                day = build(msgs, n, s)
                runs.append(surveil_windows(day))
            th, slots = n * hours, n * runs[0]["windows"]
            totals = [sum(r["alerts"].values()) for r in runs]
            out[str(n)] = {
                **day.stats, "trader_hours": th, "alerts_total_by_seed": totals,
                "alerts_per_trader_hour": round(mean(totals) / th, 4),
                "alerts_mean": {k: mean([r["alerts"][k] for r in runs]) for k in runs[0]["alerts"]},
                "wash_by_type_mean": {k: mean([r["wash_by_type"].get(k, 0) for r in runs])
                                      for k in sorted({k for r in runs for k in r["wash_by_type"]})},
                "flagged_share_of_trader_windows": round(mean([r["flagged_trader_windows"] for r in runs]) / slots, 4),
                "otr_flagged_share_of_trader_windows": round(mean([r["otr_flagged_trader_windows"] for r in runs]) / slots, 4),
                "examples": runs[0]["examples"],
            }
            log(f"{ticker} {n:>4} traders: alerts {totals} ({mean(totals) / th:.3f} per trader-hour) "
                f"{out[str(n)]['alerts_mean']} OTR flags {out[str(n)]['otr_flagged_share_of_trader_windows']:.0%} of trader-windows")
            del day, runs
        del msgs
    return res


def main(data: str, traders: Sequence[int] = (10, 50, 200), out: str = "results/real_data.json") -> dict:
    res = experiments(data, traders)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(res, indent=2))
    return res
