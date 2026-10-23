"""
VPIN (Volume-Synchronized Probability of Informed Trading) Order-Flow Toxicity
and Pre-Block-Trade Front-Running Detector.

Reference:
  Easley, D., López de Prado, M. M., & O'Hara, M. (2012).
  Flow Toxicity and Liquidity in a High-Frequency World.
  Review of Financial Studies, 25(5), 1457-1493.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .lob import TradeFill


@dataclass
class VPINBucket:
    """Single equal-volume bucket used in VPIN calculation."""

    bucket_index: int
    start_ts_ms: int
    end_ts_ms: int
    open_price: float
    close_price: float
    vwap: float
    target_volume: int
    buy_volume: float
    sell_volume: float
    bvc_buy_volume: float
    bvc_sell_volume: float
    abs_order_imbalance: float
    vpin: float
    toxicity_cdf: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VPINResult:
    """Output of Volume-Synchronized Probability of Informed Trading analysis."""

    bucket_volume: int
    window_buckets: int
    buckets: List[VPINBucket] = field(default_factory=list)
    max_vpin: float = 0.0
    mean_vpin: float = 0.0
    toxic_bucket_indices: List[int] = field(default_factory=list)

    def vpin_at_timestamp(self, timestamp_ms: int) -> float:
        if not self.buckets:
            return 0.0
        best = self.buckets[0].vpin
        for b in self.buckets:
            if b.start_ts_ms <= timestamp_ms <= b.end_ts_ms:
                return b.vpin
            if b.end_ts_ms <= timestamp_ms:
                best = b.vpin
        return best

    def to_dict(self) -> dict:
        return {
            "bucket_volume": self.bucket_volume,
            "window_buckets": self.window_buckets,
            "max_vpin": round(self.max_vpin, 4),
            "mean_vpin": round(self.mean_vpin, 4),
            "toxic_bucket_indices": list(self.toxic_bucket_indices),
            "buckets": [b.to_dict() for b in self.buckets],
        }


@dataclass
class FrontRunningAlert:
    """Alert emitted when pre-block-trade front-running is detected."""

    alert_id: str
    detector: str  # "FRONT_RUNNING"
    trader_id: str
    account_id: str
    ip_address: str
    victim_trader_id: str
    direction: str  # "BUY" or "SELL"
    start_ts_ms: int
    end_ts_ms: int
    pre_accumulated_qty: int
    block_sweep_qty: int
    unwound_qty: int
    unwind_ratio: float
    avg_entry_price: float
    avg_exit_price: float
    realized_edge_bps: float
    realized_pnl: float
    vpin_at_event: float
    confidence: float
    evidence_order_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def compute_vpin(
    fills: List[TradeFill],
    bucket_volume: int = 500,
    window_buckets: int = 8,
    toxicity_threshold: float = 0.60,
    classification: str = "aggressor",
) -> VPINResult:
    """
    Computes Volume-Synchronized Probability of Informed Trading (VPIN)
    across equal-volume buckets.

    Supports both direct Level-3 aggressor classification ("aggressor") and
    Bulk Volume Classification ("bvc") via standardized price changes.
    """
    if not fills:
        return VPINResult(bucket_volume=bucket_volume, window_buckets=window_buckets)

    sorted_fills = sorted(fills, key=lambda f: f.timestamp_ms)
    total_vol = sum(f.qty for f in sorted_fills)
    if bucket_volume <= 0:
        bucket_volume = max(100, total_vol // 20)

    raw_buckets: List[dict] = []
    cur_buy = 0.0
    cur_sell = 0.0
    cur_notional = 0.0
    cur_vol = 0
    cur_start_ts: Optional[int] = None
    cur_open_price: Optional[float] = None

    for fill in sorted_fills:
        rem_fill = fill.qty
        while rem_fill > 0:
            if cur_vol == 0:
                cur_start_ts = fill.timestamp_ms
                cur_open_price = fill.price

            space = bucket_volume - cur_vol
            take = min(rem_fill, space)
            if fill.aggressor_side.upper() == "BUY":
                cur_buy += take
            else:
                cur_sell += take
            cur_notional += take * fill.price
            cur_vol += take
            rem_fill -= take

            if cur_vol >= bucket_volume:
                raw_buckets.append(
                    {
                        "start_ts_ms": cur_start_ts if cur_start_ts is not None else fill.timestamp_ms,
                        "end_ts_ms": fill.timestamp_ms,
                        "open_price": cur_open_price if cur_open_price is not None else fill.price,
                        "close_price": fill.price,
                        "vwap": cur_notional / float(bucket_volume),
                        "buy_volume": cur_buy,
                        "sell_volume": cur_sell,
                    }
                )
                cur_buy = 0.0
                cur_sell = 0.0
                cur_notional = 0.0
                cur_vol = 0
                cur_start_ts = None
                cur_open_price = None

    if not raw_buckets:
        return VPINResult(bucket_volume=bucket_volume, window_buckets=window_buckets)

    # Compute price changes for Bulk Volume Classification (BVC)
    dp_list: List[float] = []
    for i, rb in enumerate(raw_buckets):
        prev_p = raw_buckets[i - 1]["close_price"] if i > 0 else rb["open_price"]
        dp_list.append(rb["close_price"] - prev_p)

    mean_dp = sum(dp_list) / len(dp_list)
    var_dp = sum((d - mean_dp) ** 2 for d in dp_list) / max(1, len(dp_list) - 1)
    sigma_dp = math.sqrt(var_dp) if var_dp > 1e-12 else 0.01

    imbalances: List[float] = []
    vpin_values: List[float] = []
    buckets: List[VPINBucket] = []

    for i, rb in enumerate(raw_buckets):
        z = dp_list[i] / sigma_dp if sigma_dp > 0 else 0.0
        bvc_frac_buy = _norm_cdf(z)
        bvc_buy = bucket_volume * bvc_frac_buy
        bvc_sell = bucket_volume * (1.0 - bvc_frac_buy)

        if classification.lower() == "bvc":
            used_buy, used_sell = bvc_buy, bvc_sell
        else:
            used_buy, used_sell = rb["buy_volume"], rb["sell_volume"]

        abs_oi = abs(used_buy - used_sell)
        imbalances.append(abs_oi)

        w_start = max(0, i - window_buckets + 1)
        window_slice = imbalances[w_start : i + 1]
        vpin_val = sum(window_slice) / float(len(window_slice) * bucket_volume)
        vpin_values.append(vpin_val)

        buckets.append(
            VPINBucket(
                bucket_index=i,
                start_ts_ms=rb["start_ts_ms"],
                end_ts_ms=rb["end_ts_ms"],
                open_price=round(rb["open_price"], 4),
                close_price=round(rb["close_price"], 4),
                vwap=round(rb["vwap"], 4),
                target_volume=bucket_volume,
                buy_volume=round(rb["buy_volume"], 2),
                sell_volume=round(rb["sell_volume"], 2),
                bvc_buy_volume=round(bvc_buy, 2),
                bvc_sell_volume=round(bvc_sell, 2),
                abs_order_imbalance=round(abs_oi, 2),
                vpin=round(vpin_val, 4),
                toxicity_cdf=0.0,
            )
        )

    # Populate empirical CDF of VPIN values
    sorted_vpins = sorted(vpin_values)
    n_b = len(sorted_vpins)
    toxic_indices: List[int] = []
    for b in buckets:
        rank = sum(1 for v in sorted_vpins if v <= b.vpin + 1e-12)
        b.toxicity_cdf = round(rank / float(n_b), 4)
        if b.vpin >= toxicity_threshold:
            toxic_indices.append(b.bucket_index)

    return VPINResult(
        bucket_volume=bucket_volume,
        window_buckets=window_buckets,
        buckets=buckets,
        max_vpin=max(vpin_values) if vpin_values else 0.0,
        mean_vpin=(sum(vpin_values) / len(vpin_values)) if vpin_values else 0.0,
        toxic_bucket_indices=toxic_indices,
    )


def detect_front_running(
    fills: List[TradeFill],
    vpin_result: Optional[VPINResult] = None,
    block_min_qty: int = 1200,
    sweep_cluster_ms: int = 2500,
    pre_window_ms: int = 15000,
    post_window_ms: int = 15000,
    min_front_run_qty: int = 200,
    min_unwind_ratio: float = 0.60,
    min_edge_bps: float = 8.0,
) -> List[FrontRunningAlert]:
    """
    Detects pre-block-trade front-running where a predatory trader accumulates
    directional inventory seconds before a large institutional block/sweep and
    unwinds into or immediately after the institutional sweep at an elevated price.
    """
    if not fills:
        return []

    sorted_fills = sorted(fills, key=lambda f: f.timestamp_ms)
    if vpin_result is None:
        vpin_result = compute_vpin(sorted_fills)

    # Step 1: Identify institutional directional sweep clusters
    sweeps: List[dict] = []
    n = len(sorted_fills)
    idx = 0
    while idx < n:
        f0 = sorted_fills[idx]
        trader = f0.aggressor_trader_id
        side = f0.aggressor_side.upper()
        cluster_fills = [f0]
        j = idx + 1
        while j < n and (sorted_fills[j].timestamp_ms - f0.timestamp_ms) <= sweep_cluster_ms:
            fj = sorted_fills[j]
            if fj.aggressor_trader_id == trader and fj.aggressor_side.upper() == side:
                cluster_fills.append(fj)
            j += 1

        total_sweep_qty = sum(cf.qty for cf in cluster_fills)
        if total_sweep_qty >= block_min_qty:
            sweeps.append(
                {
                    "victim_trader_id": trader,
                    "direction": side,
                    "start_ts_ms": cluster_fills[0].timestamp_ms,
                    "end_ts_ms": cluster_fills[-1].timestamp_ms,
                    "qty": total_sweep_qty,
                    "fills": cluster_fills,
                }
            )
            # Advance past this cluster
            last_ts = cluster_fills[-1].timestamp_ms
            while idx < n and sorted_fills[idx].timestamp_ms <= last_ts:
                idx += 1
        else:
            idx += 1

    alerts: List[FrontRunningAlert] = []
    seen_keys = set()

    for sw in sweeps:
        victim = sw["victim_trader_id"]
        direction = sw["direction"]
        sw_start = sw["start_ts_ms"]
        sw_end = sw["end_ts_ms"]

        pre_start = sw_start - pre_window_ms
        post_end = sw_end + post_window_ms

        # Group pre-sweep and post-sweep trades by candidate trader
        candidate_traders: Dict[str, dict] = {}

        for f in sorted_fills:
            if f.timestamp_ms < pre_start or f.timestamp_ms > post_end:
                continue

            # Evaluate participation as buyer and seller
            for role_side, t_id, a_id, ip_addr, ord_id in (
                ("BUY", f.buyer_trader_id, f.buyer_account_id, f.buyer_ip,
                 f.aggressor_order_id if f.aggressor_side == "BUY" else f.passive_order_id),
                ("SELL", f.seller_trader_id, f.seller_account_id, f.seller_ip,
                 f.aggressor_order_id if f.aggressor_side == "SELL" else f.passive_order_id),
            ):
                if t_id == victim:
                    continue
                info = candidate_traders.setdefault(
                    t_id,
                    {
                        "account_id": a_id,
                        "ip_address": ip_addr,
                        "pre_same_qty": 0,
                        "pre_same_notional": 0.0,
                        "pre_opp_qty": 0,
                        "post_unwind_qty": 0,
                        "post_unwind_notional": 0.0,
                        "order_ids": [],
                        "first_ts": f.timestamp_ms,
                        "last_ts": f.timestamp_ms,
                    },
                )
                if f.timestamp_ms < sw_start:
                    if role_side == direction:
                        info["pre_same_qty"] += f.qty
                        info["pre_same_notional"] += f.qty * f.price
                        info["order_ids"].append(ord_id)
                        info["first_ts"] = min(info["first_ts"], f.timestamp_ms)
                    else:
                        info["pre_opp_qty"] += f.qty
                else:
                    # During or immediately after the institutional sweep: look for unwind on opposite side
                    if role_side != direction:
                        info["post_unwind_qty"] += f.qty
                        info["post_unwind_notional"] += f.qty * f.price
                        info["order_ids"].append(ord_id)
                        info["last_ts"] = max(info["last_ts"], f.timestamp_ms)

        for t_id, info in candidate_traders.items():
            net_pre = info["pre_same_qty"] - info["pre_opp_qty"]
            if net_pre < min_front_run_qty or info["pre_same_qty"] <= 0:
                continue
            unwound = info["post_unwind_qty"]
            if unwound <= 0:
                continue
            unwind_ratio = unwound / float(net_pre)
            if unwind_ratio < min_unwind_ratio:
                continue

            avg_entry = info["pre_same_notional"] / float(info["pre_same_qty"])
            avg_exit = info["post_unwind_notional"] / float(unwound)
            if direction == "BUY":
                edge_bps = ((avg_exit - avg_entry) / avg_entry) * 10000.0
                matched_qty = min(net_pre, unwound)
                pnl = matched_qty * (avg_exit - avg_entry)
            else:
                edge_bps = ((avg_entry - avg_exit) / avg_entry) * 10000.0
                matched_qty = min(net_pre, unwound)
                pnl = matched_qty * (avg_entry - avg_exit)

            if edge_bps < min_edge_bps:
                continue

            dedup_key = (t_id, victim, direction, sw_start // 10000)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            vpin_val = vpin_result.vpin_at_timestamp(sw_end)
            conf = min(
                0.99,
                0.65
                + 0.15 * min(1.0, unwind_ratio)
                + 0.10 * min(1.0, edge_bps / 30.0)
                + 0.10 * min(1.0, vpin_val / 0.70),
            )
            alerts.append(
                FrontRunningAlert(
                    alert_id=f"FR-{len(alerts) + 1:04d}",
                    detector="FRONT_RUNNING",
                    trader_id=t_id,
                    account_id=info["account_id"],
                    ip_address=info["ip_address"],
                    victim_trader_id=victim,
                    direction=direction,
                    start_ts_ms=info["first_ts"],
                    end_ts_ms=info["last_ts"],
                    pre_accumulated_qty=net_pre,
                    block_sweep_qty=sw["qty"],
                    unwound_qty=unwound,
                    unwind_ratio=round(unwind_ratio, 4),
                    avg_entry_price=round(avg_entry, 4),
                    avg_exit_price=round(avg_exit, 4),
                    realized_edge_bps=round(edge_bps, 2),
                    realized_pnl=round(pnl, 2),
                    vpin_at_event=round(vpin_val, 4),
                    confidence=round(conf, 4),
                    evidence_order_ids=sorted(set(info["order_ids"])),
                )
            )

    return alerts
