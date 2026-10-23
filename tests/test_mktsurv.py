"""
Comprehensive Test Suite for `mktsurv` (Project 15: Level-3 Market Manipulation Surveillance).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mktsurv.api import app
from mktsurv.bench import format_benchmark_table, run_benchmark
from mktsurv.detectors import (
    detect_marking_the_close,
    detect_spoofing_layering,
    detect_wash_trading,
    run_surveillance,
)
from mktsurv.lob import LimitOrderBook, OrderEvent
from mktsurv.simulate import generate_market_session
from mktsurv.vpin import compute_vpin, detect_front_running


def test_lob_price_time_priority_and_microstructure_metrics() -> None:
    lob = LimitOrderBook(symbol="TEST")

    # Add two bids at $100.00 (FIFO queue) and one bid at $99.98
    lob.process_event(
        OrderEvent("ADD", "B1", "T1", "A1", "10.0.0.1", "BUY", 100.00, 300, 1000)
    )
    lob.process_event(
        OrderEvent("ADD", "B2", "T2", "A2", "10.0.0.2", "BUY", 100.00, 100, 1010)
    )
    lob.process_event(
        OrderEvent("ADD", "B3", "T3", "A3", "10.0.0.3", "BUY", 99.98, 200, 1020)
    )

    # Add ask at $100.04 (qty 200)
    lob.process_event(
        OrderEvent("ADD", "S1", "T4", "A4", "10.0.0.4", "SELL", 100.04, 200, 1030)
    )

    assert lob.best_bid() == 100.00
    assert lob.best_ask() == 100.04
    assert lob.best_bid_qty() == 400
    assert lob.best_ask_qty() == 200
    assert lob.mid_price() == pytest.approx(100.02)

    # Micro-price = (400 * 100.04 + 200 * 100.00) / 600 = 60016 / 600 = 100.026667
    assert lob.micro_price() == pytest.approx((400 * 100.04 + 200 * 100.00) / 600.0)

    # OBI top-5: V_bid = 600, V_ask = 200 -> (600 - 200) / 800 = +0.50
    assert lob.order_book_imbalance(levels=5) == pytest.approx(0.50)

    # Distance from touch on B3 ($99.98 vs $100.00 best bid) = 2.0 bps
    assert lob.lifecycles["B3"].distance_from_touch_bps == pytest.approx(2.0)

    # Incoming SELL limit at $100.00 for 350 shares should fill B1 completely (300) and B2 partially (50)
    fills = lob.process_event(
        OrderEvent("ADD", "S_AGGR", "T5", "A5", "10.0.0.5", "SELL", 100.00, 350, 1100)
    )
    assert len(fills) == 2
    assert fills[0].passive_order_id == "B1"
    assert fills[0].qty == 300
    assert fills[1].passive_order_id == "B2"
    assert fills[1].qty == 50

    assert lob.lifecycles["B1"].status == "FILLED"
    assert lob.lifecycles["B1"].fill_ratio == pytest.approx(1.0)
    assert lob.lifecycles["B2"].status == "PARTIALLY_FILLED"
    assert lob.lifecycles["B2"].remaining_qty == 50

    # Modify B3 quantity down and then cancel B2 remaining
    lob.process_event(
        OrderEvent("MODIFY", "B3", "T3", "A3", "10.0.0.3", "BUY", 99.98, 120, 1200)
    )
    assert lob.lifecycles["B3"].remaining_qty == 120
    assert lob.lifecycles["B3"].canceled_qty == 80

    lob.process_event(
        OrderEvent("CANCEL", "B2", "T2", "A2", "10.0.0.2", "BUY", 100.00, 50, 1250)
    )
    assert lob.lifecycles["B2"].time_to_cancel_ms == 240
    assert lob.best_bid() == 99.98


def test_spoofing_and_layering_detector_vs_legitimate_twap() -> None:
    lob = LimitOrderBook(symbol="SPF")
    # Background book
    lob.process_event(OrderEvent("ADD", "MM_B", "MM1", "A_MM", "10.0.0.1", "BUY", 99.99, 200, 100))
    lob.process_event(OrderEvent("ADD", "MM_S", "MM1", "A_MM", "10.0.0.1", "SELL", 100.01, 200, 110))

    # Legitimate TWAP trader places and cancels 10 BUY slices and executes on BUY (never spoofs SELL)
    for i in range(10):
        oid = f"TWAP_{i}"
        lob.process_event(
            OrderEvent("ADD", oid, "TWAP_LEGIT", "A_TWAP", "10.0.0.9", "BUY", 99.98, 300, 200 + i * 100)
        )
        lob.process_event(
            OrderEvent("CANCEL", oid, "TWAP_LEGIT", "A_TWAP", "10.0.0.9", "BUY", 99.98, 300, 250 + i * 100)
        )
    lob.process_event(
        OrderEvent("ADD", "TWAP_EXEC", "TWAP_LEGIT", "A_TWAP", "10.0.0.9", "BUY", 100.01, 100, 1300)
    )

    # Malicious Layerer places 3 large SELL layers (total 2,400 shares -> directional SELL OBI > 0.55),
    # executes a BUY trade at $100.00 against a lured seller, and cancels all 3 SELL layers within 400 ms!
    for lvl, px in enumerate([100.01, 100.02, 100.03]):
        lob.process_event(
            OrderEvent("ADD", f"LAY_{lvl}", "BAD_LAYERER", "A_BAD", "45.1.1.1", "SELL", px, 800, 2000 + lvl * 20)
        )
    lob.process_event(
        OrderEvent("ADD", "LURED_SELL", "VICTIM_1", "A_VIC", "10.0.0.8", "SELL", 100.00, 150, 2080)
    )
    lob.process_event(
        OrderEvent("ADD", "LAY_EXEC_BUY", "BAD_LAYERER", "A_BAD", "45.1.1.1", "BUY", 100.00, 150, 2100)
    )
    for lvl, px in enumerate([100.01, 100.02, 100.03]):
        lob.process_event(
            OrderEvent("CANCEL", f"LAY_{lvl}", "BAD_LAYERER", "A_BAD", "45.1.1.1", "SELL", px, 800, 2400 + lvl * 20)
        )

    alerts = detect_spoofing_layering(lob)
    flagged_traders = {a.trader_id for a in alerts}
    assert "BAD_LAYERER" in flagged_traders
    assert "TWAP_LEGIT" not in flagged_traders
    assert "MM1" not in flagged_traders
    assert alerts[0].detector == "LAYERING"
    assert alerts[0].spoof_levels_count == 3
    assert alerts[0].spoof_cancel_ratio >= 0.99
    assert alerts[0].post_fill_cancel_delay_ms <= 400


def test_wash_trading_ring_and_self_trade_detector() -> None:
    lob = LimitOrderBook(symbol="WSH")
    # 3-hop circular wash ring: WA -> WB -> WC -> WA (500 shares each leg)
    lob.process_event(OrderEvent("ADD", "O1", "WA", "ACC_A", "192.0.2.1", "SELL", 50.00, 500, 1000))
    lob.process_event(OrderEvent("ADD", "O2", "WB", "ACC_B", "192.0.2.2", "BUY", 50.00, 500, 1010))

    lob.process_event(OrderEvent("ADD", "O3", "WB", "ACC_B", "192.0.2.2", "SELL", 50.00, 500, 2000))
    lob.process_event(OrderEvent("ADD", "O4", "WC", "ACC_C", "192.0.2.3", "BUY", 50.00, 500, 2010))

    lob.process_event(OrderEvent("ADD", "O5", "WC", "ACC_C", "192.0.2.3", "SELL", 50.00, 500, 3000))
    lob.process_event(OrderEvent("ADD", "O6", "WA", "ACC_A", "192.0.2.1", "BUY", 50.00, 500, 3010))

    alerts = detect_wash_trading(lob.fills)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.detector == "COLLUSIVE_WASH_RING_3HOP"
    assert set(a.traders) == {"WA", "WB", "WC"}
    assert a.gross_volume == 1500
    assert a.net_inventory_change == 0
    assert a.net_to_gross_ratio == 0.0


def test_marking_the_close_and_vpin_front_running() -> None:
    scenario = generate_market_session(symbol="ACME", seed=42)
    lob, report = run_surveillance(
        scenario.events,
        symbol=scenario.symbol,
        vpin_bucket_volume=500,
        session_end_ts_ms=scenario.session_end_ts_ms,
    )

    # Verify Marking-the-Close alert
    assert len(report.closing_ramp_alerts) == 1
    mtc = report.closing_ramp_alerts[0]
    assert mtc.trader_id == "CLOSER_01"
    assert mtc.closing_volume_share > 0.40
    assert mtc.price_deviation_bps > 35.0

    # Verify VPIN & Front-Running alert
    assert len(report.vpin.buckets) >= 10
    assert report.vpin.max_vpin > 0.60
    assert len(report.front_running_alerts) == 1
    fr = report.front_running_alerts[0]
    assert fr.trader_id == "FRONTRUNNER_01"
    assert fr.victim_trader_id == "INST_BLOCK_BUYER"
    # The simulator buys at ~100.03 and unwinds at ~100.26 (scaled), about +24 bps
    assert 15.0 < fr.realized_edge_bps < 40.0
    assert fr.unwind_ratio >= 0.99

    # Also test BVC mode in compute_vpin
    vpin_bvc = compute_vpin(lob.fills, bucket_volume=500, classification="bvc")
    assert len(vpin_bvc.buckets) == len(report.vpin.buckets)


def test_benchmark_exceeds_95_f1_with_zero_hard_negative_fps() -> None:
    bench = run_benchmark(seed=42)
    table_str = format_benchmark_table(bench)
    assert "mktsurv" in table_str

    mktsurv_stats = bench["mktsurv"]
    naive_stats = bench["naive_baseline"]

    # Requirement: mktsurv achieves >95% F1 while reducing false positives on legitimate MMs and TWAP algos to 0%
    assert mktsurv_stats["f1_score"] > 0.95
    assert mktsurv_stats["precision"] == 1.0
    assert mktsurv_stats["recall"] == 1.0
    assert mktsurv_stats["hard_negative_fp_count"] == 0
    assert mktsurv_stats["hard_negative_fp_rate"] == 0.0
    assert bench["episode_detection_rate"] == 1.0

    # Naive OTR > 15 baseline flags legitimate MM/TWAP/Iceberg flow and has low F1
    assert naive_stats["hard_negative_fp_rate"] >= 0.50
    assert naive_stats["f1_score"] < 0.50


def test_fastapi_endpoints() -> None:
    client = TestClient(app)

    r_root = client.get("/")
    assert r_root.status_code == 200
    assert "mktsurv" in r_root.text

    r_health = client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json()["port"] == 8015

    r_bench = client.get("/bench")
    assert r_bench.status_code == 200
    assert r_bench.json()["mktsurv"]["f1_score"] > 0.95

    r_eps = client.get("/episodes")
    assert r_eps.status_code == 200
    eps_data = r_eps.json()
    assert len(eps_data["episodes"]) == 8

    r_ep = client.get("/episodes/EP-SPOOF-01")
    assert r_ep.status_code == 200
    assert r_ep.json()["detected_by_mktsurv"] is True

    r_404 = client.get("/episodes/NON_EXISTENT")
    assert r_404.status_code == 404

    r_surv = client.post("/surveil", json={"symbol": "ACME", "vpin_bucket_volume": 500})
    assert r_surv.status_code == 200
    assert len(r_surv.json()["report"]["unified_alerts"]) >= 6


def test_chance_trade_triangle_between_unrelated_traders_is_not_a_wash_ring() -> None:
    # Seed 1 used to flag RETAIL_01/03/05: three unrelated trades of 195/191/185 shares
    # over 40 s that happen to close a loop. No shared IP and no repetition: not wash trading.
    scenario = generate_market_session(symbol="ACME", seed=1)
    _, report = run_surveillance(scenario.events, symbol="ACME", vpin_bucket_volume=500,
                                 session_end_ts_ms=scenario.session_end_ts_ms)
    assert not (set(report.flagged_traders) & set(scenario.benign_traders))
    rings = [a for a in report.wash_trading_alerts if a.detector == "COLLUSIVE_WASH_RING_3HOP"]
    assert [sorted(a.traders) for a in rings] == [["RING_A", "RING_B", "RING_C"]]


def test_full_wash_ring_replaces_an_earlier_partial_alert() -> None:
    # Seed 10: an early window held only RING_A -> RING_B (shared IP, 566 sh) and its alert
    # used to suppress the full RING_A -> RING_B -> RING_C ring found in a later window.
    scenario = generate_market_session(symbol="ACME", seed=10)
    _, report = run_surveillance(scenario.events, symbol="ACME", vpin_bucket_volume=500,
                                 session_end_ts_ms=scenario.session_end_ts_ms)
    assert {"RING_A", "RING_B", "RING_C"} <= set(report.flagged_traders)
    ids = [a.alert_id for a in report.wash_trading_alerts]
    assert len(ids) == len(set(ids))


def test_lob_rejects_events_it_would_misprocess() -> None:
    lob = LimitOrderBook(symbol="BAD")
    lob.process_event(OrderEvent("ADD", "S1", "A", "a", "1", "SELL", 100.0, 100, 1))
    bad = [
        OrderEvent("ADD", "B1", "B", "b", "2", "BUY", float("nan"), 100, 2),  # used to fill like a market order
        OrderEvent("ADD", "S1", "C", "c", "3", "SELL", 102.0, 100, 3),  # reused order ID
        OrderEvent("ADD", "N1", "B", "b", "2", "BUY", 99.0, -50, 4),
        OrderEvent("BOGUS", "X1", "B", "b", "2", "BUY", 99.0, 10, 5),
        OrderEvent("ADD", "Z1", "B", "b", "2", "SIDEWAYS", 99.0, 10, 6),
    ]
    for ev in bad:
        with pytest.raises(ValueError):
            lob.process_event(ev)
    assert lob.fills == [] and list(lob.asks) == [100.0]


def test_multi_session_sweep_has_no_false_positives() -> None:
    from mktsurv.bench import run_sweep

    sweep = run_sweep(sessions=20, inclusion_prob=0.6)
    assert sweep["mktsurv"]["false_positives"] == 0
    assert sweep["mktsurv"]["recall"] == 1.0
    assert sweep["naive_baseline"]["hard_negative_fp_rate"] > 0.3
