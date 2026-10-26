import pandas as pd

from mktsurv.realdata import OPEN_MS, build, load, surveil_windows


def test_lobster_messages_become_lifecycles_and_fills(tmp_path) -> None:
    t0 = OPEN_MS / 1000
    msgs = [  # time, type, order id, size, price x 10^4, direction (1 buy, -1 sell)
        (t0 + 1.0, 1, 11, 300, 1_000_000, 1),     # bid 300 @ 100.00
        (t0 + 1.5, 1, 12, 200, 1_000_100, -1),    # ask 200 @ 100.01
        (t0 + 1.6, 1, 13, 100, 1_000_200, -1),    # ask 100 @ 100.02
        (t0 + 2.0, 2, 11, 100, 1_000_000, 1),     # partial cancel of the bid
        (t0 + 3.0, 4, 12, 200, 1_000_100, -1),    # one buyer sweeps both asks...
        (t0 + 3.0, 4, 13, 50, 1_000_200, -1),     # ...at the same timestamp
        (t0 + 3.5, 5, 0, 70, 999_900, 1),         # hidden buy order executed by a seller
        (t0 + 4.0, 3, 99, 400, 990_000, 1),       # deletes an order placed before 9:30
        (t0 + 5.0, 3, 11, 200, 1_000_000, 1),     # bid deleted
    ]
    book = [x for lvl in range(10) for x in (1_000_100 + 100 * lvl, 100, 1_000_000 - 100 * lvl, 150)]  # ask px, sz, bid px, sz
    (tmp_path / "X_message_10.csv").write_text("\n".join(",".join(map(str, m)) for m in msgs))
    pd.DataFrame([book] * len(msgs)).to_csv(tmp_path / "X_orderbook_10.csv", header=False, index=False)

    m = load(tmp_path / "X_message_10.csv", tmp_path / "X_orderbook_10.csv")
    assert m["obi"].iloc[0] == (750 - 500) / 1250                # top 5 levels, as lob.py computes it
    day = build(m, n_traders=3, seed=1)
    lc = day.lifecycles
    assert (lc["11"].canceled_qty, lc["11"].status, lc["11"].canceled_ts_ms) == (300, "CANCELED", OPEN_MS + 5000)
    assert (lc["12"].status, lc["13"].filled_qty, lc["13"].status) == ("FILLED", 50, "PARTIALLY_FILLED")
    assert lc["99"].placed_ts_ms == OPEN_MS and day.stats["unknown_order_refs"] == 1
    sweep = [f for f in day.fills if f.timestamp_ms == OPEN_MS + 3000]
    assert len(sweep) == 2 and len({f.aggressor_order_id for f in sweep}) == 1     # one aggressor
    assert all(f.aggressor_side == "BUY" and f.buyer_trader_id != f.seller_trader_id for f in sweep)
    hidden = [f for f in day.fills if f.passive_order_id.startswith("HID-")]
    assert len(hidden) == 1 and hidden[0].aggressor_side == "SELL" and hidden[0].qty == 70
    assert surveil_windows(day)["windows"] == 26


def test_sweep_over_every_pseudo_traders_orders_is_split_not_self_matched(tmp_path) -> None:
    t0 = OPEN_MS / 1000
    asks = [(t0 + 1.0 + i / 10, 1, 20 + i, 100, 1_000_100, -1) for i in range(8)]   # 8 asks @ 100.01
    sweep = [(t0 + 3.0, 4, 20 + i, 100, 1_000_100, -1) for i in range(8)]           # one buyer takes them all
    book = [x for lvl in range(10) for x in (1_000_100 + 100 * lvl, 100, 1_000_000 - 100 * lvl, 150)]
    (tmp_path / "X_message_10.csv").write_text("\n".join(",".join(map(str, m)) for m in asks + sweep))
    pd.DataFrame([book] * 16).to_csv(tmp_path / "X_orderbook_10.csv", header=False, index=False)

    day = build(load(tmp_path / "X_message_10.csv", tmp_path / "X_orderbook_10.csv"), n_traders=2, seed=0)
    assert len({lc.trader_id for oid, lc in day.lifecycles.items() if not oid.startswith("AGG-")}) == 2
    assert all(f.buyer_trader_id != f.seller_trader_id for f in day.fills)
    assert len({f.aggressor_order_id for f in day.fills}) == 1 + day.stats["split_sweeps"] > 1
