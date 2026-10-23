"""
Realistic, Seeded Level-3 Limit Order Book Stream Simulator.

Generates a full trading session containing:
  - Background Market Makers (`MM_01`, `MM_02`) with high quote update/cancel churn
  - Retail Noise Traders (`RETAIL_01` .. `RETAIL_06`) with balanced two-sided flow
  - Legitimate Institutional Execution Algos (Hard Negatives):
      * `INST_TWAP_01` (high cancel-and-replace BUY slices, never layers opposite side)
      * `INST_ICEBERG_01` (high cancel-and-replace SELL slices, never layers opposite side)
  - Ground-Truth Injected Manipulation Episodes:
      1. `EP-SPOOF-01`: Single-level Spoofing by `SPOOFER_01`
      2. `EP-LAYER-01`: 3-4 level Layering by `LAYERER_01`
      3. `EP-WASH-SELF-01`: Direct Self-Trade Wash by `WASH_SELF_01`
      4. `EP-WASH-RING-01`: 3-Account Circular Wash Ring (`RING_A` -> `RING_B` -> `RING_C` -> `RING_A`)
      5. `EP-FRONTRUN-01`: Pre-Block-Trade Front-Running by `FRONTRUNNER_01` ahead of `INST_BLOCK_BUYER`
      6. `EP-CLOSE-01`: Marking-the-Close aggressive ramp by `CLOSER_01` in final 120s

Every seed produces a *different* session: the instrument price level (1x-2.5x), per-episode
time offsets, order sizes, TWAP/Iceberg slice counts, cancel latencies and spoof cancel delays
are all randomised, and (optionally) each manipulation episode is included only with
probability `episode_inclusion_prob`, so sessions also contain "absent pattern" negatives.
Detectors never see tags or trader names — labels are only used for scoring.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from .lob import OrderEvent


@dataclass
class GroundTruthEpisode:
    """Metadata describing an injected ground-truth manipulation episode or hard-negative algo."""

    episode_id: str
    pattern: str
    sub_type: str
    is_manipulation: bool
    traders: List[str]
    start_ts_ms: int
    end_ts_ms: int
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SimulationScenario:
    """Complete simulated L3 order event stream + ground-truth labels."""

    symbol: str
    session_start_ts_ms: int
    session_end_ts_ms: int
    events: List[OrderEvent]
    episodes: List[GroundTruthEpisode]
    manipulative_traders: List[str]
    hard_negative_traders: List[str]
    benign_traders: List[str]
    seed: int = 42
    price_scale: float = 1.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "seed": self.seed,
            "price_scale": round(self.price_scale, 4),
            "session_start_ts_ms": self.session_start_ts_ms,
            "session_end_ts_ms": self.session_end_ts_ms,
            "total_events": len(self.events),
            "episodes": [e.to_dict() for e in self.episodes],
            "manipulative_traders": list(self.manipulative_traders),
            "hard_negative_traders": list(self.hard_negative_traders),
            "benign_traders": list(self.benign_traders),
        }


MANIPULATION_EPISODES: Tuple[str, ...] = (
    "EP-SPOOF-01",
    "EP-LAYER-01",
    "EP-WASH-SELF-01",
    "EP-WASH-RING-01",
    "EP-FRONTRUN-01",
    "EP-CLOSE-01",
)


def generate_market_session(
    symbol: str = "ACME",
    seed: int = 42,
    episode_inclusion_prob: float = 1.0,
    include_episodes: Optional[List[str]] = None,
) -> SimulationScenario:
    """
    Generates a seeded 15-minute (900,000 ms) Level-3 order stream with realistic market
    microstructure, hard-negative institutional/MM algos, and up to 6 manipulation episodes.

    Args:
        symbol: instrument symbol.
        seed: RNG seed; every seed yields a different price level, timing, and sizing.
        episode_inclusion_prob: probability each manipulation episode is injected.
        include_episodes: explicit list of episode IDs to inject (overrides the probability).
    """
    rng = random.Random(seed)
    price_scale = rng.uniform(1.0, 2.5)

    # Decide which manipulation episodes are injected (rng consumed in fixed order)
    draws = {ep: rng.random() for ep in MANIPULATION_EPISODES}
    if include_episodes is not None:
        included = {ep: (ep in include_episodes) for ep in MANIPULATION_EPISODES}
    else:
        included = {ep: draws[ep] < episode_inclusion_prob for ep in MANIPULATION_EPISODES}

    events: List[OrderEvent] = []
    seq = 0
    state: Dict[str, float] = {"shift": 0, "qscale": 1.0}

    def next_oid(prefix: str) -> str:
        nonlocal seq
        seq += 1
        return f"{prefix}-{seq:05d}"

    def _q(qty: int) -> int:
        return max(1, int(round(qty * state["qscale"])))

    def _px(price: float) -> float:
        return round(price * price_scale, 2)

    def add_order(
        ts: int,
        trader: str,
        acct: str,
        ip: str,
        side: str,
        price: float,
        qty: int,
        tag: str | None = None,
    ) -> str:
        oid = next_oid(trader)
        events.append(
            OrderEvent(
                event_type="ADD",
                order_id=oid,
                trader_id=trader,
                account_id=acct,
                ip_address=ip,
                side=side,
                price=_px(price),
                qty=_q(qty),
                timestamp_ms=int(ts + state["shift"]),
                tag=tag,
            )
        )
        return oid

    def cancel_order(
        ts: int,
        oid: str,
        trader: str,
        acct: str,
        ip: str,
        side: str,
        price: float = 0.0,
        qty: int = 0,
        tag: str | None = None,
    ) -> None:
        events.append(
            OrderEvent(
                event_type="CANCEL",
                order_id=oid,
                trader_id=trader,
                account_id=acct,
                ip_address=ip,
                side=side,
                price=_px(price),
                qty=_q(qty),
                timestamp_ms=int(ts + state["shift"]),
                tag=tag,
            )
        )

    def _episode(shift: int, qscale: float) -> None:
        state["shift"] = shift
        state["qscale"] = qscale

    shifts: Dict[str, int] = {}

    # -------------------------------------------------------------------------
    # Phase 0 (t = 1,000 .. 90,000 ms): Initial Book Depth + MM Quote Churn + Retail Flow
    # -------------------------------------------------------------------------
    _episode(0, 1.0)
    for lvl in range(1, 5):
        b_px = round(100.00 - 0.01 * lvl, 2)
        a_px = round(100.00 + 0.01 * lvl, 2)
        add_order(1_000 + lvl, "MM_01", "ACCT-MM01", "10.0.1.10", "BUY", b_px, 100, "MM_QUOTE")
        add_order(1_010 + lvl, "MM_01", "ACCT-MM01", "10.0.1.10", "SELL", a_px, 100, "MM_QUOTE")
        add_order(1_020 + lvl, "MM_02", "ACCT-MM02", "10.0.1.11", "BUY", b_px, 100, "MM_QUOTE")
        add_order(1_030 + lvl, "MM_02", "ACCT-MM02", "10.0.1.11", "SELL", a_px, 100, "MM_QUOTE")

    # Market makers perform frequent symmetric quote updates & cancels (high OTR > 16, cancel rate > 85%)
    mm_cycles = rng.randint(18, 26)
    mm_cancel_delay = rng.randint(500, 1200)
    for cycle in range(mm_cycles):
        t_c = 5_000 + cycle * 3_000
        oid_b1 = add_order(t_c, "MM_01", "ACCT-MM01", "10.0.1.10", "BUY", 99.95, 250, "MM_CHURN")
        oid_s1 = add_order(t_c + 10, "MM_01", "ACCT-MM01", "10.0.1.10", "SELL", 100.05, 250, "MM_CHURN")
        oid_b2 = add_order(t_c + 20, "MM_02", "ACCT-MM02", "10.0.1.11", "BUY", 99.94, 250, "MM_CHURN")
        oid_s2 = add_order(t_c + 30, "MM_02", "ACCT-MM02", "10.0.1.11", "SELL", 100.06, 250, "MM_CHURN")
        cancel_order(t_c + mm_cancel_delay, oid_b1, "MM_01", "ACCT-MM01", "10.0.1.10", "BUY", 99.95, 250, "MM_CHURN")
        cancel_order(t_c + mm_cancel_delay + 10, oid_s1, "MM_01", "ACCT-MM01", "10.0.1.10", "SELL", 100.05, 250, "MM_CHURN")
        cancel_order(t_c + mm_cancel_delay + 20, oid_b2, "MM_02", "ACCT-MM02", "10.0.1.11", "BUY", 99.94, 250, "MM_CHURN")
        cancel_order(t_c + mm_cancel_delay + 30, oid_s2, "MM_02", "ACCT-MM02", "10.0.1.11", "SELL", 100.06, 250, "MM_CHURN")

    # Balanced two-sided retail trades around 100.00 (alternating BUY and SELL aggressors)
    retail_traders = [
        ("RETAIL_01", "ACCT-R01", "172.16.0.1"),
        ("RETAIL_02", "ACCT-R02", "172.16.0.2"),
        ("RETAIL_03", "ACCT-R03", "172.16.0.3"),
        ("RETAIL_04", "ACCT-R04", "172.16.0.4"),
        ("RETAIL_05", "ACCT-R05", "172.16.0.5"),
        ("RETAIL_06", "ACCT-R06", "172.16.0.6"),
    ]
    for idx_r in range(12):
        t_r = 10_000 + idx_r * 6_000 + rng.randint(0, 1500)
        rt_b = retail_traders[idx_r % len(retail_traders)]
        rt_s = retail_traders[(idx_r + 1) % len(retail_traders)]
        r_qty = rng.randint(80, 180)
        if idx_r % 2 == 0:
            add_order(t_r, rt_s[0], rt_s[1], rt_s[2], "SELL", 100.00, r_qty, "RETAIL")
            add_order(t_r + 50, rt_b[0], rt_b[1], rt_b[2], "BUY", 100.00, r_qty, "RETAIL")
        else:
            add_order(t_r, rt_b[0], rt_b[1], rt_b[2], "BUY", 100.00, r_qty, "RETAIL")
            add_order(t_r + 50, rt_s[0], rt_s[1], rt_s[2], "SELL", 100.00, r_qty, "RETAIL")

    # -------------------------------------------------------------------------
    # Hard Negative 1 (t = 95,000 .. ~148,000 ms): Legitimate Institutional TWAP (`INST_TWAP_01`)
    # Places passive BUY slices, cancels unfilled slices to re-peg, and executes ONLY on BUY.
    # High OTR (> 18) and high cancel ratio (> 88%), but 0 opposite-side spoofing!
    # -------------------------------------------------------------------------
    n_twap = rng.randint(18, 26)
    twap_cancel = rng.randint(600, 1300)
    twap_qty = rng.randint(200, 400)
    for slice_idx in range(n_twap):
        t_twap = 95_000 + slice_idx * 2_000
        oid_twap = add_order(t_twap, "INST_TWAP_01", "ACCT-TWAP01", "192.168.10.20", "BUY", 99.98, twap_qty, "HARD_NEG_TWAP")
        cancel_order(t_twap + twap_cancel, oid_twap, "INST_TWAP_01", "ACCT-TWAP01", "192.168.10.20", "BUY", 99.98, twap_qty, "HARD_NEG_TWAP")
    t_twap_fill = 95_000 + n_twap * 2_000 + 300
    add_order(t_twap_fill, "MM_01", "ACCT-MM01", "10.0.1.10", "SELL", 100.00, 200, "MM_QUOTE")
    add_order(t_twap_fill + 100, "INST_TWAP_01", "ACCT-TWAP01", "192.168.10.20", "BUY", 100.00, 200, "HARD_NEG_TWAP")

    # -------------------------------------------------------------------------
    # Hard Negative 2 (t = 150,000 .. ~191,000 ms): Legitimate Institutional Iceberg (`INST_ICEBERG_01`)
    # Repeatedly posts and cancels/updates SELL display slices, executing ONLY on SELL.
    # -------------------------------------------------------------------------
    n_ice = rng.randint(16, 20)
    ice_cancel = rng.randint(500, 1200)
    ice_qty = rng.randint(180, 320)
    for slice_idx in range(n_ice):
        t_ice = 150_000 + slice_idx * 2_000
        oid_ice = add_order(t_ice, "INST_ICEBERG_01", "ACCT-ICE01", "192.168.10.21", "SELL", 100.02, ice_qty, "HARD_NEG_ICEBERG")
        cancel_order(t_ice + ice_cancel, oid_ice, "INST_ICEBERG_01", "ACCT-ICE01", "192.168.10.21", "SELL", 100.02, ice_qty, "HARD_NEG_ICEBERG")
    t_ice_fill = 150_000 + n_ice * 2_000 + 500
    add_order(t_ice_fill, "MM_02", "ACCT-MM02", "10.0.1.11", "BUY", 100.00, 180, "MM_QUOTE")
    add_order(t_ice_fill + 100, "INST_ICEBERG_01", "ACCT-ICE01", "192.168.10.21", "SELL", 100.00, 180, "HARD_NEG_ICEBERG")

    # -------------------------------------------------------------------------
    # Episode 1 (~t = 220,000 ms): Single-Level Spoofing (`SPOOFER_01`)
    # Places a massive phantom BUY order one tick below the touch (creating OBI > +0.55),
    # executes a bona-fide SELL at 100.00 against a lured buyer, and cancels 100% of the
    # spoof order within < 2,000 ms of the SELL fill.
    # -------------------------------------------------------------------------
    shifts["EP-SPOOF-01"] = rng.randint(-15_000, 15_000)
    spoof_cancel_delay = rng.randint(150, 1500)
    if included["EP-SPOOF-01"]:
        _episode(shifts["EP-SPOOF-01"], rng.uniform(1.0, 1.5))
        spoof_oid = add_order(220_000, "SPOOFER_01", "ACCT-SPF01", "45.33.12.90", "BUY", 99.99, 3600, "EP-SPOOF-01")
        add_order(220_250, "RETAIL_01", "ACCT-R01", "172.16.0.1", "BUY", 100.00, 300, "RETAIL_LURED")
        add_order(220_400, "SPOOFER_01", "ACCT-SPF01", "45.33.12.90", "SELL", 100.00, 300, "EP-SPOOF-01")
        cancel_order(220_400 + spoof_cancel_delay, spoof_oid, "SPOOFER_01", "ACCT-SPF01", "45.33.12.90", "BUY", 99.99, 3600, "EP-SPOOF-01")

    # -------------------------------------------------------------------------
    # Episode 2 (~t = 310,000 ms): Multi-Level Layering (`LAYERER_01`)
    # Layers 3-4 price levels on the SELL side, executes a BUY at 100.00 on the opposite
    # side, and cancels all SELL layers within < 2,000 ms.
    # -------------------------------------------------------------------------
    shifts["EP-LAYER-01"] = rng.randint(-15_000, 15_000)
    n_layers = rng.randint(3, 4)
    layer_qtys = [rng.randint(1000, 1400) for _ in range(n_layers)]
    layer_cancel_delay = rng.randint(200, 1200)
    if included["EP-LAYER-01"]:
        _episode(shifts["EP-LAYER-01"], rng.uniform(1.0, 1.4))
        layer_oids: List[Tuple[str, float, int]] = []
        for idx_lvl in range(n_layers):
            px = round(100.01 + 0.01 * idx_lvl, 2)
            q_l = layer_qtys[idx_lvl]
            loid = add_order(310_000 + idx_lvl * 40, "LAYERER_01", "ACCT-LAY01", "45.33.12.99", "SELL", px, q_l, "EP-LAYER-01")
            layer_oids.append((loid, px, q_l))
        add_order(310_300, "RETAIL_02", "ACCT-R02", "172.16.0.2", "SELL", 100.00, 350, "RETAIL_LURED")
        add_order(310_450, "LAYERER_01", "ACCT-LAY01", "45.33.12.99", "BUY", 100.00, 350, "EP-LAYER-01")
        for idx_lvl, (loid, px, q_l) in enumerate(layer_oids):
            cancel_order(310_450 + layer_cancel_delay + idx_lvl * 30, loid, "LAYERER_01", "ACCT-LAY01", "45.33.12.99", "SELL", px, q_l, "EP-LAYER-01")

    # -------------------------------------------------------------------------
    # Episode 3 (~t = 410,500 ms): Direct Self-Trade Wash (`WASH_SELF_01`)
    # -------------------------------------------------------------------------
    shifts["EP-WASH-SELF-01"] = rng.randint(-20_000, 20_000)
    if included["EP-WASH-SELF-01"]:
        _episode(shifts["EP-WASH-SELF-01"], rng.uniform(0.7, 1.5))
        add_order(410_500, "WASH_SELF_01", "ACCT-WSH01", "185.20.4.7", "SELL", 100.00, 750, "EP-WASH-SELF-01")
        add_order(410_550, "WASH_SELF_01", "ACCT-WSH01", "185.20.4.7", "BUY", 100.00, 750, "EP-WASH-SELF-01")

    # -------------------------------------------------------------------------
    # Episode 4 (~t = 482,000 .. 490,020 ms): 3-Account Circular Wash Ring
    # (`RING_A` -> `RING_B` -> `RING_C` -> `RING_A`)
    # High gross volume, 0 net inventory change (< 5% net/gross).
    # -------------------------------------------------------------------------
    shifts["EP-WASH-RING-01"] = rng.randint(-10_000, 10_000)
    if included["EP-WASH-RING-01"]:
        _episode(shifts["EP-WASH-RING-01"], rng.uniform(0.8, 1.5))
        add_order(482_000, "RING_A", "ACCT-RA", "198.51.100.14", "SELL", 100.00, 450, "EP-WASH-RING-01")
        add_order(482_020, "RING_B", "ACCT-RB", "198.51.100.14", "BUY", 100.00, 450, "EP-WASH-RING-01")
        add_order(486_000, "RING_B", "ACCT-RB", "198.51.100.14", "SELL", 100.00, 450, "EP-WASH-RING-01")
        add_order(486_020, "RING_C", "ACCT-RC", "198.51.100.15", "BUY", 100.00, 450, "EP-WASH-RING-01")
        add_order(490_000, "RING_C", "ACCT-RC", "198.51.100.15", "SELL", 100.00, 450, "EP-WASH-RING-01")
        add_order(490_020, "RING_A", "ACCT-RA", "198.51.100.14", "BUY", 100.00, 450, "EP-WASH-RING-01")

    # -------------------------------------------------------------------------
    # Episode 5 (~t = 612,000 .. 615,000 ms): Pre-Block-Trade Front-Running (`FRONTRUNNER_01`)
    # 1. `FRONTRUNNER_01` accumulates 500 shares of BUY inventory ~3 s before the block sweep.
    # 2. `INST_BLOCK_BUYER` executes a 2,000-share aggressive BUY sweep (spiking VPIN toxicity).
    # 3. `FRONTRUNNER_01` unwinds into `INST_BLOCK_BUYER` ~23 ticks higher (~+24 bps edge).
    # The block sweep itself always happens (benign institutional flow).
    # -------------------------------------------------------------------------
    shifts["EP-FRONTRUN-01"] = rng.randint(-10_000, 10_000)
    _episode(shifts["EP-FRONTRUN-01"], 1.0)
    if included["EP-FRONTRUN-01"]:
        add_order(612_000, "FRONTRUNNER_01", "ACCT-FR01", "203.0.113.88", "BUY", 100.03, 500, "EP-FRONTRUN-01")
    add_order(614_000, "MM_01", "ACCT-MM01", "10.0.1.10", "SELL", 100.10, 600, "MM_QUOTE")
    add_order(614_050, "MM_02", "ACCT-MM02", "10.0.1.11", "SELL", 100.18, 600, "MM_QUOTE")
    if included["EP-FRONTRUN-01"]:
        add_order(614_200, "FRONTRUNNER_01", "ACCT-FR01", "203.0.113.88", "SELL", 100.26, 500, "EP-FRONTRUN-01")
    add_order(615_000, "INST_BLOCK_BUYER", "ACCT-INST-BLK", "192.168.50.10", "BUY", 100.28, 2000, "INST_BLOCK_SWEEP")

    # Balanced two-sided trading around 100.04 between t = 650,000 .. 720,000 ms
    _episode(0, 1.0)
    for idx_m in range(6):
        t_m = 650_000 + idx_m * 10_000 + rng.randint(0, 2000)
        rt_b = retail_traders[idx_m % len(retail_traders)]
        rt_s = retail_traders[(idx_m + 2) % len(retail_traders)]
        m_qty = rng.randint(100, 200)
        if idx_m % 2 == 0:
            add_order(t_m, rt_b[0], rt_b[1], rt_b[2], "BUY", 100.04, m_qty, "RETAIL")
            add_order(t_m + 50, rt_s[0], rt_s[1], rt_s[2], "SELL", 100.04, m_qty, "RETAIL")
        else:
            add_order(t_m, rt_s[0], rt_s[1], rt_s[2], "SELL", 100.04, m_qty, "RETAIL")
            add_order(t_m + 50, rt_b[0], rt_b[1], rt_b[2], "BUY", 100.04, m_qty, "RETAIL")

    # -------------------------------------------------------------------------
    # Episode 6 (final 120 s of the 900,000 ms session): Marking-the-Close (`CLOSER_01`)
    # Aggressive BUY sweeps representing > 40% of closing-window volume that ramp the
    # closing price ~+60 bps above the 15-minute VWAP. Without the closer, the passive
    # ask ladder simply rests and the close stays near VWAP.
    # -------------------------------------------------------------------------
    shifts["EP-CLOSE-01"] = rng.randint(-4_000, 0)
    _episode(shifts["EP-CLOSE-01"], rng.uniform(1.0, 1.4))
    add_order(800_000, "RETAIL_03", "ACCT-R03", "172.16.0.3", "SELL", 100.05, 150, "RETAIL_CLOSE")
    add_order(800_100, "RETAIL_04", "ACCT-R04", "172.16.0.4", "BUY", 100.05, 150, "RETAIL_CLOSE")
    add_order(830_000, "RETAIL_05", "ACCT-R05", "172.16.0.5", "BUY", 100.06, 150, "RETAIL_CLOSE")
    add_order(830_100, "RETAIL_06", "ACCT-R06", "172.16.0.6", "SELL", 100.06, 150, "RETAIL_CLOSE")
    add_order(860_000, "MM_01", "ACCT-MM01", "10.0.1.10", "SELL", 100.25, 300, "MM_CLOSE_ASK")
    add_order(860_050, "MM_02", "ACCT-MM02", "10.0.1.11", "SELL", 100.45, 350, "MM_CLOSE_ASK")
    add_order(860_100, "MM_01", "ACCT-MM01", "10.0.1.10", "SELL", 100.68, 450, "MM_CLOSE_ASK")
    if included["EP-CLOSE-01"]:
        add_order(885_000, "CLOSER_01", "ACCT-CLS01", "198.18.0.44", "BUY", 100.30, 300, "EP-CLOSE-01")
        add_order(892_000, "CLOSER_01", "ACCT-CLS01", "198.18.0.44", "BUY", 100.50, 350, "EP-CLOSE-01")
        add_order(898_000, "CLOSER_01", "ACCT-CLS01", "198.18.0.44", "BUY", 100.70, 450, "EP-CLOSE-01")
    _episode(0, 1.0)

    # Stable sort keeps causal order for events sharing a timestamp
    events.sort(key=lambda e: e.timestamp_ms)

    def _s(ep: str, t: int) -> int:
        return t + shifts.get(ep, 0)

    manip_catalog = [
        GroundTruthEpisode(
            episode_id="EP-SPOOF-01",
            pattern="SPOOFING_LAYERING",
            sub_type="SPOOFING",
            is_manipulation=True,
            traders=["SPOOFER_01"],
            start_ts_ms=_s("EP-SPOOF-01", 220_000),
            end_ts_ms=_s("EP-SPOOF-01", 220_400 + spoof_cancel_delay),
            description=(
                "Single-level spoofing: phantom BUY wall one tick below the touch (OBI > 0.55), "
                f"opposite SELL execution, 100% cancel {spoof_cancel_delay} ms after the fill."
            ),
        ),
        GroundTruthEpisode(
            episode_id="EP-LAYER-01",
            pattern="SPOOFING_LAYERING",
            sub_type="LAYERING",
            is_manipulation=True,
            traders=["LAYERER_01"],
            start_ts_ms=_s("EP-LAYER-01", 310_000),
            end_ts_ms=_s("EP-LAYER-01", 310_450 + layer_cancel_delay + 30 * n_layers),
            description=(
                f"{n_layers}-level ASK layering ({sum(layer_qtys)} base shares), opposite BUY fill at 100.00, "
                f"all layers cancelled within {layer_cancel_delay + 30 * n_layers} ms."
            ),
        ),
        GroundTruthEpisode(
            episode_id="EP-WASH-SELF-01",
            pattern="WASH_TRADING",
            sub_type="SELF_TRADE",
            is_manipulation=True,
            traders=["WASH_SELF_01"],
            start_ts_ms=_s("EP-WASH-SELF-01", 410_500),
            end_ts_ms=_s("EP-WASH-SELF-01", 410_550),
            description="Direct self-trade by WASH_SELF_01 with zero net position change.",
        ),
        GroundTruthEpisode(
            episode_id="EP-WASH-RING-01",
            pattern="WASH_TRADING",
            sub_type="COLLUSIVE_WASH_RING_3HOP",
            is_manipulation=True,
            traders=["RING_A", "RING_B", "RING_C"],
            start_ts_ms=_s("EP-WASH-RING-01", 482_000),
            end_ts_ms=_s("EP-WASH-RING-01", 490_020),
            description="3-account circular wash ring (RING_A -> RING_B -> RING_C -> RING_A) with 0 net inventory change.",
        ),
        GroundTruthEpisode(
            episode_id="EP-FRONTRUN-01",
            pattern="FRONT_RUNNING",
            sub_type="FRONT_RUNNING",
            is_manipulation=True,
            traders=["FRONTRUNNER_01"],
            start_ts_ms=_s("EP-FRONTRUN-01", 612_000),
            end_ts_ms=_s("EP-FRONTRUN-01", 615_000),
            description="Pre-block-trade front-running: 500 shares accumulated ~3 s before a 2,000-share block sweep, unwound into the sweep (~+24 bps).",
        ),
        GroundTruthEpisode(
            episode_id="EP-CLOSE-01",
            pattern="MARKING_THE_CLOSE",
            sub_type="MARKING_THE_CLOSE",
            is_manipulation=True,
            traders=["CLOSER_01"],
            start_ts_ms=_s("EP-CLOSE-01", 885_000),
            end_ts_ms=_s("EP-CLOSE-01", 898_000),
            description="Closing-bell ramp: CLOSER_01 dominates final-120s volume, pushing the close > 35 bps above 15m VWAP.",
        ),
    ]
    episodes = [ep for ep in manip_catalog if included[ep.episode_id]]
    episodes += [
        GroundTruthEpisode(
            episode_id="HN-TWAP-01",
            pattern="LEGITIMATE_ALGO",
            sub_type="INSTITUTIONAL_TWAP",
            is_manipulation=False,
            traders=["INST_TWAP_01"],
            start_ts_ms=95_000,
            end_ts_ms=t_twap_fill + 100,
            description=f"Hard negative: legitimate TWAP buyer, {n_twap} cancel/re-peg slices on the same side.",
        ),
        GroundTruthEpisode(
            episode_id="HN-ICEBERG-01",
            pattern="LEGITIMATE_ALGO",
            sub_type="INSTITUTIONAL_ICEBERG",
            is_manipulation=False,
            traders=["INST_ICEBERG_01"],
            start_ts_ms=150_000,
            end_ts_ms=t_ice_fill + 100,
            description=f"Hard negative: legitimate Iceberg seller, {n_ice} cancel/replace display slices.",
        ),
    ]

    manipulative_traders: List[str] = []
    for ep in episodes:
        if ep.is_manipulation:
            manipulative_traders.extend(ep.traders)

    hard_negative_traders = ["MM_01", "MM_02", "INST_TWAP_01", "INST_ICEBERG_01"]
    benign_traders = hard_negative_traders + [
        "INST_BLOCK_BUYER",
        "RETAIL_01",
        "RETAIL_02",
        "RETAIL_03",
        "RETAIL_04",
        "RETAIL_05",
        "RETAIL_06",
    ]

    return SimulationScenario(
        symbol=symbol,
        session_start_ts_ms=1_000,
        session_end_ts_ms=900_000,
        events=events,
        episodes=episodes,
        manipulative_traders=manipulative_traders,
        hard_negative_traders=hard_negative_traders,
        benign_traders=benign_traders,
        seed=seed,
        price_scale=price_scale,
    )
