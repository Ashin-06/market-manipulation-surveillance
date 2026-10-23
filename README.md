# Market Manipulation Surveillance (`mktsurv`)

This replays level-3 order events (every add, cancel, modify and market order, with trader, account and IP) through a price-time-priority matching engine. Rule-based detectors then run over the order lifecycles and fills, looking for spoofing and layering, wash trading, marking the close, and front-running around large trades. The comparison baseline is the classic order-to-trade-ratio rule (OTR > 15 or cancel rate > 85%), which flags busy market makers and execution algorithms.

The benchmark below comes from a built-in simulator. "Real data" further down runs the detectors over real Nasdaq order flow.

## What it does

**Order book** (`lob.py`). A single-instrument book with price-time priority. It tracks each order's fills, cancels, time-to-cancel and distance from the touch, plus the book imbalance (OBI) when the order was placed. It reports mid, micro-price and spread. Events with unknown types or sides, non-finite or negative prices, non-positive quantities or reused order IDs are rejected. (A NaN-priced limit order used to trade like a market order.)

**Detectors** (`detectors.py`, `vpin.py`). All are threshold rules with hand-set parameters.
- *Spoofing / layering.* A trader rests at least 500 shares on one side while book imbalance toward that side exceeds 0.55, trades on the other side, then cancels more than 80% of the resting orders within 2 seconds of that trade. Two or more price levels count as layering.
- *Wash trading.* Covers self-trades (same trader or account on both sides) and 2- and 3-party loops within 60 seconds whose net position change is under 5% of gross. A loop is reported only with evidence of prearrangement: a shared IP, every leg traded at least twice, or identically sized legs. Without that rule, chance loops among unrelated retail traders were flagged 111 times in 200 sessions.
- *Marking the close.* One trader's aggressive orders make up over 40% of volume in the last 120 seconds and move the close more than 35 bps from the preceding 15-minute VWAP.
- *Front-running.* A trader builds a position in the 15 seconds before a block of at least 1,200 shares from someone else, then unwinds at least 60% of it into the block at an edge of 8 bps or more. VPIN (Easley, López de Prado and O'Hara) is reported alongside the alert but is not part of the rule.

**Simulator** (`simulate.py`). This is a 15-minute session with two market makers, six retail traders, and two legitimate institutional algos as hard negatives: a TWAP and an iceberg, both with high cancel rates. Up to six manipulation episodes are injected, one per detector pattern (spoof, layer, self-trade, three-account ring, front-run, close ramp). Each seed changes price level, timing, order sizes, slice counts and cancel latencies, and each episode can be left out at random. Detectors never see trader names or tags; those are used only for scoring.

## Results

`python -m mktsurv bench` scores one session (seed 42), then a sweep of 200 sessions with each episode injected at p = 0.6. Scoring is per trader.

| 200 sessions | Precision | Recall | False positives | Hard negatives flagged |
|---|---|---|---|---|
| mktsurv | 100% | 100% (952/952) | 0 | 0% |
| OTR > 15 / cancel > 85% | 0% | 0% | 478 | 59.8% |

Every injected episode was detected: spoof 122/122, layer 112/112, self-trade 121/121, ring 122/122, front-run 114/114, close ramp 117/117. Separate sweeps with every episode always included (p = 1.0) and with none (p = 0.0) also produced no false positives and no misses. The seed-42 session alone gives 8 true positives, 0 false positives, and 2 of 4 hard negatives flagged by the OTR rule.

**What this shows and what it doesn't.** Every session reuses the same six episode templates, shifted and rescaled, and the thresholds were set with those templates and these hard negatives in view. The 100% therefore means the detectors survive the simulator's variation in timing, size and price level. It says nothing about manipulation strategies they weren't written for. For false alerts on genuine traffic, see the next section.

## Real data

`python -m mktsurv realdata --data DIR` runs the detectors over LOBSTER's free Nasdaq samples: every add, cancel and execution within the top 10 price levels of AAPL, AMZN, GOOG, INTC and MSFT on 21 June 2012, 9:30 to 16:00. That is 74,000–331,000 orders and 11,000–35,000 fills per stock. Results are in `results/real_data.json`. There are no manipulation labels, so this counts false alerts on (presumably) genuine traffic and says nothing about recall.
- **No re-matching.** Nasdaq already matched these orders, so order lifecycles and fills are built straight from the messages. Book imbalance at placement comes from LOBSTER's own 10-level book.
- **The traders are made up.** The data has no trader, account or IP fields. Each order goes to one of N pseudo-traders at random. Each burst of executions with the same timestamp and side becomes one aggressor order from another random pseudo-trader that owns none of the orders it hits. Any alert is therefore a coincidence of real order flow and counts as false. Real ownership would cluster each firm's quoting and could give more or fewer alerts. Results are means over 3 random attributions.
- **Windows.** The detectors run on 26 fifteen-minute windows (the simulator's session length); marking the close runs on the last one.

False alerts per trader-hour, all detectors:

| Stock (price) | 10 traders | 50 traders | 200 traders |
|---|---|---|---|
| AAPL ($582) | 19.4 | 0.04 | 0.003 |
| AMZN ($223) | 1.5 | 0.02 | 0.0003 |
| GOOG ($569) | 1.1 | 0.007 | 0.0008 |
| INTC ($27) | 17.2 | 0.93 | 0.22 |
| MSFT ($31) | 17.8 | 1.35 | 0.19 |

What the real data shows:
- **With ten traders, as in the simulator, wash-trade loops swamp everything**: 65–1,213 a day per stock. When a few accounts share all of a stock's real flow, 2- and 3-party loops with repeated legs or equal sizes happen by chance, so the prearrangement evidence that stopped this in the simulator doesn't hold. About 100 a day on INTC and MSFT are self-trades, an artifact of the attribution: one sweep can hit orders from all ten pseudo-traders. With 50 or more traders the loops nearly disappear.
- **Front-running doesn't carry across price levels.** On INTC and MSFT it fires 240–390 times a day with 50 or 200 traders. Its thresholds are in shares (a 1,200-share block, a 200-share position): about $36,000 of a $30 stock that trades in 300-share orders, against $700,000 of AAPL, which trades in 100s.
- **Spoofing coincidences are rare but real**: 3–72 a day with ten traders and 0–8 with 200. Real flow often has a trade followed within 2 seconds by cancellations of the same account's resting orders on the other side.
- **Marking the close** never fired, but there are only five closes here.
- **The order-to-trade baseline** flags 0–79% of trader-windows, depending mostly on the stock rather than on behaviour.

The detectors were not changed after seeing this. To be usable on real flow they would need thresholds in notional or relative to each stock's own volume, and real account attribution. Nasdaq's public feed attributes only some orders to a member firm, so that needs a venue's or broker's own records.

## Run it

```bash
pip install -r requirements.txt
python -m pytest -q                    # 12 tests
python -m mktsurv bench                # single session + 200-session sweep
python -m mktsurv realdata --data DIR  # LOBSTER sample files, about 7 minutes
python -m mktsurv serve --port 8015    # API + console at http://127.0.0.1:8015
```

Endpoints: `POST /surveil` (your own events, up to 5,000, or the simulated session), `GET /episodes`, `GET /episodes/{id}`, `GET /bench`, `GET /health`. Set `MKTSURV_API_KEY` to require it in the `X-API-Key` header (the console has a field for it). Without the key the server runs open, and `serve` refuses to bind a non-loopback address.

## Limits

- **No labelled real cases.** Recall is measured only on simulated episodes. Real order flow (see Real data) gives false-alert counts, and only under made-up trader attribution.
- **Rules, not models.** Every threshold is a hand-set constant. Real surveillance would tune them per instrument and venue and review the alerts by hand.
- **One instrument, one venue.** There is no cross-venue or cross-product view; spoofing across related instruments and wash trades through other venues are invisible. Linkage between accounts is only the IP field; real wash-trade detection needs beneficial-ownership data.
- **Simplified matching engine.** There are no auctions, hidden or iceberg order types, self-trade prevention or tick-size checks. Every query rescans the book's levels, which is fine for thousands of events but nowhere near exchange volumes.
- **Front-running needs the victim's trades in the same feed** and only looks at one block at a time.
- **No deployment story**: no TLS, rate limiting, persistence, case management or audit trail.
