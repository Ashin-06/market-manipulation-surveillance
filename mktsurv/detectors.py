"""
Microstructure Manipulation Detectors:
  1. Spoofing & Multi-Level Layering (`detect_spoofing_layering`)
  2. Self-Trade & Collusive Wash-Trading Rings (`detect_wash_trading`)
  3. Marking-the-Close / Closing-Bell Ramps (`detect_marking_the_close`)
  4. Unified Surveillance Pipeline + Naive OTR Baseline (`run_surveillance`, `naive_otr_baseline`)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .lob import LimitOrderBook, OrderEvent, OrderLifecycle, TradeFill, typical_size
from .vpin import FrontRunningAlert, VPINResult, compute_vpin, detect_front_running


@dataclass
class SpoofingAlert:
    """Alert emitted when Spoofing or Multi-Level Layering is detected."""

    alert_id: str
    detector: str  # "SPOOFING" or "LAYERING"
    trader_id: str
    account_id: str
    ip_address: str
    spoof_side: str  # Side where phantom liquidity was layered
    exec_side: str  # Opposite side where bona-fide trade executed
    start_ts_ms: int
    exec_ts_ms: int
    cancel_ts_ms: int
    spoof_levels_count: int
    spoof_total_qty: int
    spoof_canceled_qty: int
    spoof_cancel_ratio: float
    post_fill_cancel_delay_ms: int
    peak_directional_obi: float
    opposite_exec_qty: int
    confidence: float
    spoof_order_ids: List[str] = field(default_factory=list)
    exec_order_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WashTradingAlert:
    """Alert emitted for Direct Self-Trades, Shared-IP Wash Trades, or Collusive Wash Rings."""

    alert_id: str
    detector: str  # "SELF_TRADE", "SHARED_IP_WASH", "COLLUSIVE_WASH_RING_2HOP", "COLLUSIVE_WASH_RING_3HOP"
    traders: List[str]
    accounts: List[str]
    ip_addresses: List[str]
    start_ts_ms: int
    end_ts_ms: int
    gross_volume: int
    net_inventory_change: int
    net_to_gross_ratio: float
    fill_count: int
    shared_ip: bool
    confidence: float
    fill_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClosingRampAlert:
    """Alert emitted when Marking-the-Close / Closing-Bell Ramp is detected."""

    alert_id: str
    detector: str  # "MARKING_THE_CLOSE"
    trader_id: str
    account_id: str
    ip_address: str
    direction: str  # "BUY" or "SELL"
    start_ts_ms: int
    end_ts_ms: int
    trader_closing_volume: int
    total_closing_volume: int
    closing_volume_share: float
    vwap_15m: float
    closing_price: float
    price_deviation_bps: float
    aggressive_fill_ratio: float
    confidence: float
    fill_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SurveillanceAlert:
    """Normalized unified surveillance alert across all 4 detectors."""

    alert_id: str
    pattern: str
    sub_type: str
    primary_trader_id: str
    involved_traders: List[str]
    start_ts_ms: int
    end_ts_ms: int
    confidence: float
    summary: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SurveillanceReport:
    """Comprehensive surveillance output from running the LOB + all 4 detectors."""

    symbol: str
    total_events: int
    total_fills: int
    spoofing_alerts: List[SpoofingAlert]
    wash_trading_alerts: List[WashTradingAlert]
    closing_ramp_alerts: List[ClosingRampAlert]
    front_running_alerts: List[FrontRunningAlert]
    unified_alerts: List[SurveillanceAlert]
    vpin: VPINResult
    flagged_traders: List[str]
    naive_baseline_flagged_traders: List[str]

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "total_events": self.total_events,
            "total_fills": self.total_fills,
            "spoofing_alerts": [a.to_dict() for a in self.spoofing_alerts],
            "wash_trading_alerts": [a.to_dict() for a in self.wash_trading_alerts],
            "closing_ramp_alerts": [a.to_dict() for a in self.closing_ramp_alerts],
            "front_running_alerts": [a.to_dict() for a in self.front_running_alerts],
            "unified_alerts": [a.to_dict() for a in self.unified_alerts],
            "vpin": self.vpin.to_dict(),
            "flagged_traders": list(self.flagged_traders),
            "naive_baseline_flagged_traders": list(self.naive_baseline_flagged_traders),
        }


def detect_spoofing_layering(
    lob: LimitOrderBook,
    min_obi_imbalance: float = 0.55,
    max_cancel_after_fill_ms: int = 2000,
    min_cancel_ratio: float = 0.80,
    max_spoof_fill_ratio: float = 0.20,
    min_spoof_size: float = 5.0,
    lookback_ms: int = 10000,
) -> List[SpoofingAlert]:
    """
    Detects Spoofing (1 price level) and Layering (2-4+ price levels).

    Requires the joint microstructure signature:
      (a) Large non-bona-fide resting volume (at least `min_spoof_size` times the
          `typical_size` of all orders in the book) placed across 1-4+ price levels on
          `spoof_side` creating > 0.55 directional book imbalance.
      (b) Bona-fide trade execution by the SAME trader on the OPPOSITE side (`exec_side`)
          while the spoof orders are live on the book.
      (c) Rapid cancellation (< 2000 ms after opposite fill) of > 80% of the
          `spoof_side` orders immediately after the opposite execution.
    """
    alerts: List[SpoofingAlert] = []
    lifecycles = list(lob.lifecycles.values())
    min_spoof_qty = min_spoof_size * typical_size(lc.initial_qty for lc in lifecycles)

    # Index lifecycles by trader_id
    by_trader: Dict[str, List[OrderLifecycle]] = {}
    for lc in lifecycles:
        by_trader.setdefault(lc.trader_id, []).append(lc)

    # Index fills by trader_id and side
    fills_by_trader: Dict[str, List[Tuple[str, TradeFill, str]]] = {}
    for f in lob.fills:
        fills_by_trader.setdefault(f.buyer_trader_id, []).append(
            (
                "BUY",
                f,
                f.aggressor_order_id if f.aggressor_side == "BUY" else f.passive_order_id,
            )
        )
        if f.seller_trader_id != f.buyer_trader_id:
            fills_by_trader.setdefault(f.seller_trader_id, []).append(
                (
                    "SELL",
                    f,
                    f.aggressor_order_id if f.aggressor_side == "SELL" else f.passive_order_id,
                )
            )

    seen_clusters: Set[Tuple[str, str, int]] = set()

    for trader_id, trader_fills in fills_by_trader.items():
        trader_orders = by_trader.get(trader_id, [])
        if not trader_orders:
            continue

        for exec_side, fill, exec_order_id in trader_fills:
            spoof_side = "SELL" if exec_side == "BUY" else "BUY"
            exec_ts = fill.timestamp_ms

            # Find candidate spoof orders on `spoof_side` placed shortly before `exec_ts`
            # that were still live at `exec_ts` and canceled shortly after `exec_ts`.
            live_spoof_orders: List[OrderLifecycle] = []
            for lc in trader_orders:
                if lc.side != spoof_side:
                    continue
                if lc.placed_ts_ms > exec_ts or (exec_ts - lc.placed_ts_ms) > lookback_ms:
                    continue
                if lc.canceled_ts_ms is None or lc.canceled_ts_ms < exec_ts:
                    continue
                if (lc.canceled_ts_ms - exec_ts) > max_cancel_after_fill_ms:
                    continue
                if lc.fill_ratio > max_spoof_fill_ratio:
                    continue
                live_spoof_orders.append(lc)

            if not live_spoof_orders:
                continue

            total_spoof_qty = sum(o.initial_qty for o in live_spoof_orders)
            total_canceled_qty = sum(o.canceled_qty for o in live_spoof_orders)
            if total_spoof_qty < min_spoof_qty:
                continue

            cancel_ratio = total_canceled_qty / float(total_spoof_qty)
            if cancel_ratio < min_cancel_ratio:
                continue

            # Directional OBI in favor of spoof_side:
            # When spoof_side == "BUY", OBI = (V_bid - V_ask)/(V_bid + V_ask) > +0.55
            # When spoof_side == "SELL", -OBI = (V_ask - V_bid)/(V_bid + V_ask) > +0.55
            directional_obis = [
                (o.book_imbalance_at_placement if spoof_side == "BUY" else -o.book_imbalance_at_placement)
                for o in live_spoof_orders
            ]
            peak_obi = max(directional_obis) if directional_obis else 0.0
            if peak_obi <= min_obi_imbalance:
                continue

            start_ts = min(o.placed_ts_ms for o in live_spoof_orders)
            cancel_ts = max(o.canceled_ts_ms for o in live_spoof_orders if o.canceled_ts_ms is not None)
            post_fill_delay = max(0, cancel_ts - exec_ts)
            if post_fill_delay > max_cancel_after_fill_ms:
                continue

            cluster_key = (trader_id, spoof_side, start_ts // 5000)
            if cluster_key in seen_clusters:
                continue
            seen_clusters.add(cluster_key)

            # Aggregate all opposite-side executions by this trader during [start_ts, cancel_ts]
            opp_fills = [
                (f_obj, ord_id)
                for f_side, f_obj, ord_id in trader_fills
                if f_side == exec_side and start_ts <= f_obj.timestamp_ms <= cancel_ts
            ]
            opp_exec_qty = sum(f_obj.qty for f_obj, _ in opp_fills)
            exec_ord_ids = sorted(set(ord_id for _, ord_id in opp_fills))

            distinct_prices = sorted(set(o.price for o in live_spoof_orders))
            levels_count = len(distinct_prices)
            detector_type = "LAYERING" if levels_count >= 2 else "SPOOFING"

            conf = min(
                0.99,
                0.70
                + 0.10 * min(1.0, (peak_obi - min_obi_imbalance) / 0.25)
                + 0.10 * min(1.0, cancel_ratio)
                + 0.09 * min(1.0, levels_count / 3.0),
            )

            sample_lc = live_spoof_orders[0]
            alerts.append(
                SpoofingAlert(
                    alert_id=f"SPF-{len(alerts) + 1:04d}",
                    detector=detector_type,
                    trader_id=trader_id,
                    account_id=sample_lc.account_id,
                    ip_address=sample_lc.ip_address,
                    spoof_side=spoof_side,
                    exec_side=exec_side,
                    start_ts_ms=start_ts,
                    exec_ts_ms=exec_ts,
                    cancel_ts_ms=cancel_ts,
                    spoof_levels_count=levels_count,
                    spoof_total_qty=total_spoof_qty,
                    spoof_canceled_qty=total_canceled_qty,
                    spoof_cancel_ratio=round(cancel_ratio, 4),
                    post_fill_cancel_delay_ms=post_fill_delay,
                    peak_directional_obi=round(peak_obi, 4),
                    opposite_exec_qty=opp_exec_qty,
                    confidence=round(conf, 4),
                    spoof_order_ids=[o.order_id for o in live_spoof_orders],
                    exec_order_ids=exec_ord_ids,
                )
            )

    return alerts


def detect_wash_trading(
    fills: List[TradeFill],
    window_ms: int = 60000,
    min_ring_size: float = 2.0,
    min_edge_size: float = 0.5,
    max_net_to_gross_ratio: float = 0.05,
    min_ring_share: float = 0.5,
) -> List[WashTradingAlert]:
    """
    Detects:
      1. Direct Self-Trades (same trader_id or same account_id on both sides).
      2. Shared-IP/Device Counterparty Wash Trades (distinct traders sharing IP with near-zero net inventory change or repeated volume).
      3. 2-Hop and 3-Hop Collusive Wash-Trading Rings (A -> B -> A or A -> B -> C -> A)
         where gross traded volume is high but net inventory change is < 5% of gross volume.

    A balanced loop of trades between unrelated accounts happens by chance in any busy book, so a
    ring is only reported with some evidence of prearrangement: two members sharing an IP address,
    every leg traded at least twice in the window (repeated round trips), or every leg of exactly
    the same size (matched orders). Without a shared IP the loop must also be at least
    `min_ring_share` of each member's trading in the window: accounts that trade a lot in a busy
    book trade with each other both ways, in repeated or equal lots, by chance.

    Loop and leg volumes are multiples (`min_ring_size`, `min_edge_size`) of the fills'
    `typical_size`.
    """
    if not fills:
        return []

    sorted_fills = sorted(fills, key=lambda f: f.timestamp_ms)
    unit = typical_size(f.qty for f in sorted_fills)
    min_ring_volume, min_edge_volume = min_ring_size * unit, min_edge_size * unit
    alerts: List[WashTradingAlert] = []
    flagged_fill_sets: List[Set[str]] = []  # flagged_fill_sets[k] holds the fills of alerts[k]

    def _make_room(fids: Set[str]) -> bool:
        """
        False if an existing alert already covers these fills. Otherwise drop any earlier alert
        whose fills this one contains: an early window may hold only one leg of a ring, and that
        partial alert must not hide the full ring found in a later window.
        """
        if any(fids <= prev for prev in flagged_fill_sets):
            return False
        keep = [k for k, prev in enumerate(flagged_fill_sets) if not prev < fids]
        alerts[:] = [alerts[k] for k in keep]
        flagged_fill_sets[:] = [flagged_fill_sets[k] for k in keep]
        return True

    def _mostly_within(members: Tuple[str, ...], loop_fills: List[TradeFill], traded: Dict[str, int]) -> bool:
        """Each member did at least `min_ring_share` of its trading in the window inside the loop."""
        inside = dict.fromkeys(members, 0)
        for f in loop_fills:
            inside[f.buyer_trader_id] += f.qty
            inside[f.seller_trader_id] += f.qty
        return all(inside[t] >= min_ring_share * traded[t] for t in members)

    # 1. Direct Self-Trades
    self_trades_by_trader: Dict[str, List[TradeFill]] = {}
    for f in sorted_fills:
        if f.buyer_trader_id == f.seller_trader_id or f.buyer_account_id == f.seller_account_id:
            self_trades_by_trader.setdefault(f.buyer_trader_id, []).append(f)

    for trader_id, st_fills in self_trades_by_trader.items():
        gross_vol = sum(f.qty for f in st_fills)
        fids = [f.fill_id for f in st_fills]
        flagged_fill_sets.append(set(fids))
        alerts.append(
            WashTradingAlert(
                alert_id=f"WSH-{len(alerts) + 1:04d}",
                detector="SELF_TRADE",
                traders=sorted(set([st_fills[0].buyer_trader_id, st_fills[0].seller_trader_id])),
                accounts=sorted(set([st_fills[0].buyer_account_id, st_fills[0].seller_account_id])),
                ip_addresses=sorted(set([st_fills[0].buyer_ip, st_fills[0].seller_ip])),
                start_ts_ms=st_fills[0].timestamp_ms,
                end_ts_ms=st_fills[-1].timestamp_ms,
                gross_volume=gross_vol,
                net_inventory_change=0,
                net_to_gross_ratio=0.0,
                fill_count=len(st_fills),
                shared_ip=(st_fills[0].buyer_ip == st_fills[0].seller_ip),
                confidence=0.99,
                fill_ids=fids,
            )
        )

    # 2. Sliding-window directed volume graph for 2-hop / 3-hop collusive wash rings & shared-IP wash
    n = len(sorted_fills)
    seen_ring_keys: Set[Tuple[Tuple[str, ...], int]] = set()

    for i in range(n):
        w_start = sorted_fills[i].timestamp_ms
        w_end = w_start + window_ms
        window_fills = [
            f
            for f in sorted_fills[i:]
            if f.timestamp_ms <= w_end
            and f.buyer_trader_id != f.seller_trader_id
            and f.buyer_account_id != f.seller_account_id
        ]
        if len(window_fills) < 2:
            continue

        # Build directed graph: edge u -> v means u sold to v (or transferred asset to v)
        edge_vol: Dict[Tuple[str, str], int] = {}
        edge_fills: Dict[Tuple[str, str], List[TradeFill]] = {}
        trader_acct: Dict[str, str] = {}
        trader_ip: Dict[str, str] = {}
        neighbors: Dict[str, Set[str]] = {}
        traded: Dict[str, int] = {}

        for f in window_fills:
            u = f.seller_trader_id
            v = f.buyer_trader_id
            traded[u] = traded.get(u, 0) + f.qty
            traded[v] = traded.get(v, 0) + f.qty
            edge_vol[(u, v)] = edge_vol.get((u, v), 0) + f.qty
            edge_fills.setdefault((u, v), []).append(f)
            neighbors.setdefault(u, set()).add(v)
            trader_acct[u] = f.seller_account_id
            trader_acct[v] = f.buyer_account_id
            trader_ip[u] = f.seller_ip
            trader_ip[v] = f.buyer_ip

        # Check 3-hop cycles: A -> B -> C -> A
        nodes = sorted(neighbors.keys())
        for a in nodes:
            for b in sorted(neighbors.get(a, [])):
                if b <= a or edge_vol.get((a, b), 0) < min_edge_volume:
                    continue
                for c in sorted(neighbors.get(b, [])):
                    if c <= a or c == b or edge_vol.get((b, c), 0) < min_edge_volume:
                        continue
                    if a in neighbors.get(c, set()) and edge_vol.get((c, a), 0) >= min_edge_volume:
                        ring_traders = (a, b, c)
                        c_fills = (
                            edge_fills[(a, b)]
                            + edge_fills[(b, c)]
                            + edge_fills[(c, a)]
                        )
                        gross_vol = sum(f.qty for f in c_fills)
                        if gross_vol < min_ring_volume:
                            continue

                        net_pos: Dict[str, int] = {t: 0 for t in ring_traders}
                        for f in c_fills:
                            net_pos[f.buyer_trader_id] += f.qty
                            net_pos[f.seller_trader_id] -= f.qty

                        max_abs_net = max(abs(v) for v in net_pos.values())
                        net_ratio = max_abs_net / float(gross_vol)
                        if net_ratio >= max_net_to_gross_ratio:
                            continue

                        ips = sorted(set(trader_ip[t] for t in ring_traders))
                        shared_ip = len(ips) < len(ring_traders)
                        legs = ((a, b), (b, c), (c, a))
                        repeated = min(len(edge_fills[e]) for e in legs) >= 2
                        matched_sizes = len({edge_vol[e] for e in legs}) == 1
                        if not (shared_ip or ((repeated or matched_sizes) and _mostly_within(ring_traders, c_fills, traded))):
                            continue

                        fids_set = {f.fill_id for f in c_fills}
                        if not _make_room(fids_set):
                            continue

                        ring_key = (tuple(sorted(ring_traders)), min(f.timestamp_ms for f in c_fills) // 10000)
                        if ring_key in seen_ring_keys:
                            continue
                        seen_ring_keys.add(ring_key)
                        flagged_fill_sets.append(fids_set)

                        conf = min(0.99, 0.85 + 0.10 * (1.0 - net_ratio / max_net_to_gross_ratio) + (0.04 if shared_ip else 0.0))

                        alerts.append(
                            WashTradingAlert(
                                alert_id=f"WSH-{len(alerts) + 1:04d}",
                                detector="COLLUSIVE_WASH_RING_3HOP",
                                traders=list(ring_traders),
                                accounts=[trader_acct[t] for t in ring_traders],
                                ip_addresses=ips,
                                start_ts_ms=min(f.timestamp_ms for f in c_fills),
                                end_ts_ms=max(f.timestamp_ms for f in c_fills),
                                gross_volume=gross_vol,
                                net_inventory_change=max_abs_net,
                                net_to_gross_ratio=round(net_ratio, 4),
                                fill_count=len(c_fills),
                                shared_ip=shared_ip,
                                confidence=round(conf, 4),
                                fill_ids=sorted(fids_set),
                            )
                        )

        # Check 2-hop cycles (A <-> B) or Shared-IP Counterparty Wash
        for a in nodes:
            for b in sorted(neighbors.get(a, [])):
                if b <= a:
                    continue
                v_ab = edge_vol.get((a, b), 0)
                v_ba = edge_vol.get((b, a), 0)
                same_ip = trader_ip.get(a) == trader_ip.get(b)

                if (v_ab >= min_edge_volume and v_ba >= min_edge_volume) or (same_ip and (v_ab + v_ba) >= min_ring_volume):
                    c_fills = edge_fills.get((a, b), []) + edge_fills.get((b, a), [])
                    gross_vol = sum(f.qty for f in c_fills)
                    if gross_vol < min_ring_volume:
                        continue
                    net_change = abs(v_ab - v_ba)
                    net_ratio = net_change / float(gross_vol)
                    if net_ratio >= max_net_to_gross_ratio and not same_ip:
                        continue
                    repeated = min(len(edge_fills.get((a, b), [])), len(edge_fills.get((b, a), []))) >= 2
                    if not (same_ip or ((repeated or v_ab == v_ba) and _mostly_within((a, b), c_fills, traded))):
                        continue

                    fids_set = {f.fill_id for f in c_fills}
                    if not _make_room(fids_set):
                        continue

                    ring_key = ((a, b), min(f.timestamp_ms for f in c_fills) // 10000)
                    if ring_key in seen_ring_keys:
                        continue
                    seen_ring_keys.add(ring_key)
                    flagged_fill_sets.append(fids_set)

                    det_name = "SHARED_IP_WASH" if same_ip and net_ratio >= max_net_to_gross_ratio else "COLLUSIVE_WASH_RING_2HOP"
                    alerts.append(
                        WashTradingAlert(
                            alert_id=f"WSH-{len(alerts) + 1:04d}",
                            detector=det_name,
                            traders=[a, b],
                            accounts=[trader_acct[a], trader_acct[b]],
                            ip_addresses=sorted(set([trader_ip[a], trader_ip[b]])),
                            start_ts_ms=min(f.timestamp_ms for f in c_fills),
                            end_ts_ms=max(f.timestamp_ms for f in c_fills),
                            gross_volume=gross_vol,
                            net_inventory_change=net_change,
                            net_to_gross_ratio=round(net_ratio, 4),
                            fill_count=len(c_fills),
                            shared_ip=same_ip,
                            confidence=0.95 if same_ip else 0.90,
                            fill_ids=sorted(fids_set),
                        )
                    )

    for k, alert in enumerate(alerts, start=1):
        alert.alert_id = f"WSH-{k:04d}"
    return alerts


def detect_marking_the_close(
    fills: List[TradeFill],
    session_end_ts_ms: Optional[int] = None,
    closing_window_ms: int = 120_000,
    vwap_window_ms: int = 900_000,
    min_closing_volume_share: float = 0.40,
    min_price_deviation_bps: float = 35.0,
) -> List[ClosingRampAlert]:
    """
    Detects Marking-the-Close / Closing-Bell Ramps:
      Aggressive directional sweeps in the final 120 seconds of trading that
      represent > 40% of closing-window volume and push the closing price
      > 35 bps away from the 15-minute VWAP.
    """
    if not fills:
        return []

    sorted_fills = sorted(fills, key=lambda f: f.timestamp_ms)
    end_ts = session_end_ts_ms if session_end_ts_ms is not None else sorted_fills[-1].timestamp_ms
    close_start_ts = end_ts - closing_window_ms
    vwap_start_ts = end_ts - vwap_window_ms

    vwap_fills = [f for f in sorted_fills if vwap_start_ts <= f.timestamp_ms <= end_ts]
    closing_fills = [f for f in sorted_fills if close_start_ts <= f.timestamp_ms <= end_ts]
    if not closing_fills or not vwap_fills:
        return []

    # Compute 15-minute pre-close or full-window reference VWAP
    pre_close_fills = [f for f in vwap_fills if f.timestamp_ms < close_start_ts]
    ref_fills = pre_close_fills if pre_close_fills else vwap_fills
    ref_vol = sum(f.qty for f in ref_fills)
    if ref_vol <= 0:
        return []
    vwap_15m = sum(f.qty * f.price for f in ref_fills) / float(ref_vol)

    closing_price = closing_fills[-1].price
    total_closing_vol = sum(f.qty for f in closing_fills)
    if total_closing_vol <= 0:
        return []

    # Group aggressive closing fills by trader and side
    aggr_by_trader: Dict[Tuple[str, str], List[TradeFill]] = {}
    for f in closing_fills:
        key = (f.aggressor_trader_id, f.aggressor_side.upper())
        aggr_by_trader.setdefault(key, []).append(f)

    alerts: List[ClosingRampAlert] = []
    for (trader_id, side), t_fills in aggr_by_trader.items():
        trader_vol = sum(f.qty for f in t_fills)
        vol_share = trader_vol / float(total_closing_vol)
        if vol_share <= min_closing_volume_share:
            continue

        if side == "BUY":
            dev_bps = ((closing_price - vwap_15m) / vwap_15m) * 10000.0
        else:
            dev_bps = ((vwap_15m - closing_price) / vwap_15m) * 10000.0

        if dev_bps <= min_price_deviation_bps:
            continue

        # Compute aggressive ratio of this trader during the closing window
        all_trader_closing_vol = sum(
            f.qty
            for f in closing_fills
            if f.buyer_trader_id == trader_id or f.seller_trader_id == trader_id
        )
        aggr_ratio = trader_vol / float(max(1, all_trader_closing_vol))

        conf = min(
            0.99,
            0.70
            + 0.15 * min(1.0, (vol_share - min_closing_volume_share) / 0.30)
            + 0.14 * min(1.0, (dev_bps - min_price_deviation_bps) / 40.0),
        )
        f0 = t_fills[0]
        alerts.append(
            ClosingRampAlert(
                alert_id=f"MTC-{len(alerts) + 1:04d}",
                detector="MARKING_THE_CLOSE",
                trader_id=trader_id,
                account_id=f0.aggressor_account_id,
                ip_address=f0.aggressor_ip,
                direction=side,
                start_ts_ms=t_fills[0].timestamp_ms,
                end_ts_ms=t_fills[-1].timestamp_ms,
                trader_closing_volume=trader_vol,
                total_closing_volume=total_closing_vol,
                closing_volume_share=round(vol_share, 4),
                vwap_15m=round(vwap_15m, 4),
                closing_price=round(closing_price, 4),
                price_deviation_bps=round(dev_bps, 2),
                aggressive_fill_ratio=round(aggr_ratio, 4),
                confidence=round(conf, 4),
                fill_ids=[f.fill_id for f in t_fills],
            )
        )

    return alerts


def naive_otr_baseline(
    lob: LimitOrderBook,
    otr_threshold: float = 15.0,
    cancel_rate_threshold: float = 0.85,
    min_orders: int = 8,
) -> Dict[str, dict]:
    """
    Naive legacy regulatory surveillance baseline:
      Flags any trader with Order-to-Trade Ratio (OTR) > 15 or Cancel Rate > 85%
      (subject to `min_orders`).

    Demonstrates why naive ratios produce severe false positives on legitimate
    market makers and institutional TWAP/Iceberg algos, while missing wash rings,
    marking-the-close, and front-running.
    """
    stats: Dict[str, dict] = {}
    for lc in lob.lifecycles.values():
        st = stats.setdefault(
            lc.trader_id,
            {
                "trader_id": lc.trader_id,
                "orders": 0,
                "order_qty": 0,
                "canceled_orders": 0,
                "canceled_qty": 0,
                "filled_orders": 0,
                "filled_qty": 0,
            },
        )
        st["orders"] += 1
        st["order_qty"] += lc.initial_qty
        if lc.canceled_qty > 0:
            st["canceled_orders"] += 1
            st["canceled_qty"] += lc.canceled_qty
        if lc.filled_qty > 0:
            st["filled_orders"] += 1
            st["filled_qty"] += lc.filled_qty

    flagged: Dict[str, dict] = {}
    for trader_id, st in stats.items():
        if st["orders"] < min_orders:
            continue
        fills_cnt = max(1, st["filled_orders"])
        otr = st["orders"] / float(fills_cnt)
        cancel_rate = st["canceled_qty"] / float(max(1, st["order_qty"]))
        st["otr"] = round(otr, 2)
        st["cancel_rate"] = round(cancel_rate, 4)
        if otr > otr_threshold or cancel_rate > cancel_rate_threshold:
            flagged[trader_id] = st

    return flagged


def run_surveillance(
    events: Iterable[OrderEvent],
    symbol: str = "ACME",
    vpin_bucket_volume: int = 500,
    session_end_ts_ms: Optional[int] = None,
) -> Tuple[LimitOrderBook, SurveillanceReport]:
    """
    Processes a stream of Level-3 OrderEvents through the LOB matching engine
    and runs all 4 microstructure manipulation detectors.
    """
    lob = LimitOrderBook(symbol=symbol)
    lob.process_stream(events, snapshot_every=5)

    spoof_alerts = detect_spoofing_layering(lob)
    wash_alerts = detect_wash_trading(lob.fills)
    close_alerts = detect_marking_the_close(lob.fills, session_end_ts_ms=session_end_ts_ms)
    vpin_res = compute_vpin(lob.fills, bucket_volume=vpin_bucket_volume)
    fr_alerts = detect_front_running(lob.fills, vpin_result=vpin_res)
    naive_flagged = naive_otr_baseline(lob)

    unified: List[SurveillanceAlert] = []
    flagged_traders_set: Set[str] = set()

    for a in spoof_alerts:
        flagged_traders_set.add(a.trader_id)
        unified.append(
            SurveillanceAlert(
                alert_id=a.alert_id,
                pattern="SPOOFING_LAYERING",
                sub_type=a.detector,
                primary_trader_id=a.trader_id,
                involved_traders=[a.trader_id],
                start_ts_ms=a.start_ts_ms,
                end_ts_ms=a.cancel_ts_ms,
                confidence=a.confidence,
                summary=(
                    f"{a.detector} by {a.trader_id}: layered {a.spoof_total_qty} sh across "
                    f"{a.spoof_levels_count} {a.spoof_side} level(s) (OBI={a.peak_directional_obi:+.2f}), "
                    f"executed {a.opposite_exec_qty} sh on {a.exec_side}, and canceled "
                    f"{a.spoof_cancel_ratio*100:.0f}% within {a.post_fill_cancel_delay_ms} ms."
                ),
                details=a.to_dict(),
            )
        )

    for a in wash_alerts:
        for t in a.traders:
            flagged_traders_set.add(t)
        unified.append(
            SurveillanceAlert(
                alert_id=a.alert_id,
                pattern="WASH_TRADING",
                sub_type=a.detector,
                primary_trader_id=a.traders[0],
                involved_traders=list(a.traders),
                start_ts_ms=a.start_ts_ms,
                end_ts_ms=a.end_ts_ms,
                confidence=a.confidence,
                summary=(
                    f"{a.detector} among {', '.join(a.traders)}: gross={a.gross_volume} sh, "
                    f"net change={a.net_inventory_change} sh ({a.net_to_gross_ratio*100:.1f}%)."
                ),
                details=a.to_dict(),
            )
        )

    for a in close_alerts:
        flagged_traders_set.add(a.trader_id)
        unified.append(
            SurveillanceAlert(
                alert_id=a.alert_id,
                pattern="MARKING_THE_CLOSE",
                sub_type=a.detector,
                primary_trader_id=a.trader_id,
                involved_traders=[a.trader_id],
                start_ts_ms=a.start_ts_ms,
                end_ts_ms=a.end_ts_ms,
                confidence=a.confidence,
                summary=(
                    f"MARKING_THE_CLOSE by {a.trader_id}: {a.closing_volume_share*100:.1f}% of "
                    f"closing volume ({a.direction}), ramping close {a.price_deviation_bps:.1f} bps "
                    f"from 15m VWAP (${a.vwap_15m:.2f} -> ${a.closing_price:.2f})."
                ),
                details=a.to_dict(),
            )
        )

    for a in fr_alerts:
        flagged_traders_set.add(a.trader_id)
        unified.append(
            SurveillanceAlert(
                alert_id=a.alert_id,
                pattern="FRONT_RUNNING",
                sub_type=a.detector,
                primary_trader_id=a.trader_id,
                involved_traders=[a.trader_id],
                start_ts_ms=a.start_ts_ms,
                end_ts_ms=a.end_ts_ms,
                confidence=a.confidence,
                summary=(
                    f"FRONT_RUNNING by {a.trader_id} ahead of {a.victim_trader_id}: accumulated "
                    f"{a.pre_accumulated_qty} sh ({a.direction}), unwound {a.unwound_qty} sh "
                    f"(+{a.realized_edge_bps:.1f} bps, PnL=${a.realized_pnl:.2f}, VPIN={a.vpin_at_event:.2f})."
                ),
                details=a.to_dict(),
            )
        )

    unified.sort(key=lambda x: x.start_ts_ms)

    report = SurveillanceReport(
        symbol=symbol,
        total_events=len(lob.events),
        total_fills=len(lob.fills),
        spoofing_alerts=spoof_alerts,
        wash_trading_alerts=wash_alerts,
        closing_ramp_alerts=close_alerts,
        front_running_alerts=fr_alerts,
        unified_alerts=unified,
        vpin=vpin_res,
        flagged_traders=sorted(flagged_traders_set),
        naive_baseline_flagged_traders=sorted(naive_flagged.keys()),
    )
    return lob, report
