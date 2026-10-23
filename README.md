# Project 15 — Level-3 Market Manipulation Surveillance Engine (`mktsurv`)

A deterministic **Level-3 Limit Order Book (LOB) Microstructure Surveillance Engine** that reconstructs full order queues, order lifecycles, and matched trade graphs to detect **Spoofing**, **Multi-Level Layering**, **Collusive Wash-Trading Rings**, **Marking-the-Close**, and **Informed Order-Flow Toxicity (VPIN) / Pre-Block Front-Running** while eliminating false positives on legitimate Market Makers and institutional TWAP/Iceberg execution algorithms.

---

## Architecture

1. **`mktsurv/lob.py` — Deterministic Level-3 Limit Order Book Matching Engine**
   - Processes `ADD`, `CANCEL`, `MODIFY`, and `MARKET` events with strict price-time (FIFO) priority.
   - Tracks bid/ask depth queues, mid-price, volume-weighted micro-price, spread (bps), and Order Book Imbalance:
     $$\text{OBI} = \frac{V_{\text{bid}} - V_{\text{ask}}}{V_{\text{bid}} + V_{\text{ask}}}$$
   - Emits `TradeFill` execution records and `OrderLifecycle` metrics (`time_to_cancel_ms`, `distance_from_touch_bps`, `fill_ratio`, `cancel_ratio`, `book_imbalance_at_placement`).

2. **`mktsurv/detectors.py` & `mktsurv/vpin.py` — 4 Microstructure Manipulation Detectors**
   - **Spoofing & Multi-Level Layering (`detect_spoofing_layering`)**: Requires the joint microstructure signature:
     1. Large non-bona-fide resting volume across 1–4+ price levels on `spoof_side` creating $> 0.55$ directional book imbalance,
     2. Bona-fide trade execution by the *same* trader on the *opposite* side (`exec_side`) while the spoof orders are resting, and
     3. Rapid cancellation ($< 2000\text{ ms}$) of $> 80\%$ of the `spoof_side` orders immediately after the opposite execution.
   - **Self-Trade & Collusive Wash-Trading Ring Detector (`detect_wash_trading`)**: Builds a sliding-window directed volume graph over `TradeFill`s to flag direct self-trades, shared-IP counterparty trades, and 2-hop / 3-hop circular wash rings ($A \to B \to C \to A$) where gross volume is high but net inventory change is $< 5\%$ of gross volume.
   - **Marking-the-Close / Closing-Bell Ramp Detector (`detect_marking_the_close`)**: Flags aggressive directional sweeps in the final 120 seconds of trading that represent $> 40\%$ of closing-window volume and push the closing price $> 35\text{ bps}$ away from the 15-minute VWAP.
   - **VPIN Order-Flow Toxicity & Front-Running Detector (`compute_vpin`, `detect_front_running`)**: Computes Volume-Synchronized Probability of Informed Trading (Easley, López de Prado, O'Hara) across equal-volume buckets (supporting both Level-3 aggressor classification and Bulk Volume Classification) and detects pre-block-trade front-running ahead of large institutional sweeps.

3. **`mktsurv/simulate.py` & `mktsurv/bench.py` — L3 Simulator & Regulatory Benchmark**
   - Simulates a 15-minute Level-3 session with background market makers (`MM_01`, `MM_02`), retail noise traders, **hard-negative legitimate institutional TWAP (`INST_TWAP_01`) and Iceberg (`INST_ICEBERG_01`) algorithms**, and 6 injected manipulation episodes.
   - Compares `mktsurv` against a naive regulatory threshold (`Order-to-Trade Ratio > 15` / Cancel Rate $> 85\%$).

---

## Benchmark Results (`python -m mktsurv bench`)

| Metric | `mktsurv` (L3 Microstructure Engine) | Naive Regulatory Baseline (`OTR > 15`) |
| :--- | :---: | :---: |
| **Precision** | **100.00%** | 33.33% |
| **Recall** | **100.00%** | 25.00% |
| **F1 Score** | **100.00%** (`> 95%`) | 28.57% |
| **False Positive Rate on Legitimate MMs & TWAP/Iceberg** | **0.00%** (`0 / 4`) | 100.00% (`4 / 4`) |
| **Episode-Level Detection Recall** | **100.00%** (`6 / 6`) | 33.33% (`2 / 6`) |

---

## Quickstart & CLI

```bash
# Run the test suite
python -m pytest -q

# Run the Level-3 surveillance benchmark
python -m mktsurv bench

# Launch the FastAPI surveillance server & interactive console on port 8015
python -m mktsurv serve --port 8015
```
