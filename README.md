# Market Manipulation Surveillance (`mktsurv`)

This replays level-3 order events (every add, cancel, modify and market order, with trader, account and IP) through a price-time-priority matching engine. Rule-based detectors then run over the order lifecycles and fills, looking for spoofing and layering, wash trading, marking the close, and front-running around large trades. The comparison baseline is the classic order-to-trade-ratio rule (OTR > 15 or cancel rate > 85%), which flags busy market makers and execution algorithms.

The benchmark below comes from a built-in simulator. "Real data" further down runs the detectors over real Nasdaq order flow.

## What it does

**Order book** (`lob.py`). A single-instrument book with price-time priority. It tracks each order's fills, cancels, time-to-cancel and distance from the touch, plus the book imbalance (OBI) when the order was placed. It reports mid, micro-price and spread. Events with unknown types or sides, non-finite or negative prices, non-positive quantities or reused order IDs are rejected. (A NaN-priced limit order used to trade like a market order.)

**Detectors** (`detectors.py`, `vpin.py`). All are threshold rules with hand-set parameters. Sizes are not in shares but in multiples of the instrument's *typical size*: the volume-weighted median of the order or fill sizes in the data being checked (half of all volume goes through pieces that size or smaller). A plain median would not work, since most Nasdaq fills are 100 shares whether the stock costs $27 or $580. What differs is how big the large ones get: the typical fill is about 650 shares on INTC and MSFT, and 100 on AAPL, AMZN and GOOG.
- *Spoofing / layering.* A trader rests at least 5× the typical order size on one side while book imbalance toward that side exceeds 0.55, trades on the other side, then cancels more than 80% of the resting orders within 2 seconds of that trade. The trader must also be one-sided: the resting volume is at least 3× the trader's own orders live on the trade's side. A market maker also pulls a big quote right after a fill on the other side, but it rests about as much on that side. Two or more price levels count as layering.
- *Wash trading.* Covers self-trades (same trader or account on both sides) and 2- and 3-party loops within 60 seconds whose net position change is under 5% of gross. A loop is reported only with evidence of prearrangement: a shared IP; or else every leg traded at least twice, or all legs the same size at one price (matched orders), *and* the loop making up at least half of each member's trading in the window. Without the first rule, chance loops among unrelated retail traders were flagged 111 times in 200 sessions. Without the second, busy accounts in real order flow trade with each other in equal round lots all the time (see Real data).
- *Marking the close.* One trader's aggressive orders make up over 40% of volume in the last 120 seconds and move the close more than 35 bps from the preceding 15-minute VWAP.
- *Front-running.* A trader builds a position in the 15 seconds before a block (at least 3× the typical fill) from someone else, then unwinds at least 60% of it into the block at an edge of 8 bps or more. The position must be at least half the typical fill and at least half of all volume traded in those 15 seconds. VPIN (Easley, López de Prado and O'Hara) is reported alongside the alert but is not part of the rule.

**Simulator** (`simulate.py`). This is a 15-minute session with two market makers, six retail traders, and two legitimate institutional algos as hard negatives: a TWAP and an iceberg, both with high cancel rates. Up to six manipulation episodes are injected, one per detector pattern (spoof, layer, self-trade, three-account ring, front-run, close ramp). Each seed changes price level, timing, order sizes, slice counts and cancel latencies, and each episode can be left out at random. Detectors never see trader names or tags; those are used only for scoring.

## Results

`python -m mktsurv bench` scores one session (seed 42), then a sweep of 200 sessions with each episode injected at p = 0.6. Scoring is per trader.

| 200 sessions | Precision | Recall | False positives | Hard negatives flagged |
|---|---|---|---|---|
| mktsurv | 100% | 100% (952/952) | 0 | 0% |
| OTR > 15 / cancel > 85% | 0% | 0% | 478 | 59.8% |

Every injected episode was detected: spoof 122/122, layer 112/112, self-trade 121/121, ring 122/122, front-run 114/114, close ramp 117/117. Separate sweeps with every episode always included (p = 1.0) and with none (p = 0.0) also produced no false positives and no misses, as did 1,000 sessions on seeds never used for tuning (1000–1999: 4,862/4,862). The seed-42 session alone gives 8 true positives, 0 false positives, and 2 of 4 hard negatives flagged by the OTR rule.

**What this shows and what it doesn't.** Every session reuses the same six episode templates, shifted and rescaled, and the thresholds were set with those templates and these hard negatives in view. The 100% therefore means the detectors survive the simulator's variation in timing, size and price level. It says nothing about manipulation strategies they weren't written for. For false alerts on genuine traffic, see the next section.

## Real data

`python -m mktsurv realdata --data DIR` runs the detectors over LOBSTER's free Nasdaq samples: every add, cancel and execution within the top 10 price levels of AAPL, AMZN, GOOG, INTC and MSFT on 21 June 2012, 9:30 to 16:00. That is 74,000–331,000 orders and 11,000–35,000 fills per stock. Results are in `results/real_data.json`. There are no manipulation labels, so this counts false alerts on (presumably) genuine traffic and says nothing about recall.
- **No re-matching.** Nasdaq already matched these orders, so order lifecycles and fills are built straight from the messages. Book imbalance at placement comes from LOBSTER's own 10-level book.
- **The traders are made up.** The data has no trader, account or IP fields. Each order goes to one of N pseudo-traders at random. Each burst of executions with the same timestamp and side becomes one aggressor order from another random pseudo-trader that owns none of the orders it hits. A burst that hits orders of every pseudo-trader is split between aggressors, as self-match prevention would do. That happens about 150 times a day on INTC and MSFT with ten traders, and used to produce about 100 false self-trade alerts a day there. Any alert is therefore a coincidence of real order flow and counts as false. Real ownership would cluster each firm's quoting and could give more or fewer alerts. Results are means over 3 random attributions.
- **Windows.** The detectors run on 26 fifteen-minute windows (the simulator's session length); marking the close runs on the last one.

**Before and after.** The first run had every size threshold in shares (a 1,200-share block, a 200-share front-running position, 500 shares of spoof orders, 500-share wash loops). On INTC and MSFT that is routine flow, so front-running fired 240–390 times a day even with 200 traders, and with ten traders chance wash loops came to 65–1,213 a day on every stock. Since then:
1. Every size threshold is a multiple of the stock's typical size (see Detectors).
2. A front-running position must also be at least half of the volume traded in the 15 seconds before the block.
3. A wash loop must be at least half of each member's trading in the window.
4. Matched wash-loop legs must also be at one price.
5. A spoofer must be one-sided (see Detectors).

How they were chosen, and what is in-sample:
- **Changes 1–3** were tuned on the simulator and on INTC (one random attribution), so **INTC is in-sample**. On the held-out AAPL, AMZN, GOOG and MSFT, front-running and spoofing improved. Chance two-party loops rose on the three expensive stocks, though (AAPL with 50 traders: 0.04 to 0.12 per trader-hour), because twice their 100-share typical fill is just two round lots.
- **Change 4** came from looking at those AAPL loops: 27 of 29 were one 100-share lot each way, at two different prices. It has no tuned number, but it was made after seeing the first results on every stock, so for the wash rule no stock is a clean hold-out.
- **Change 5** was tuned on AAPL. Its chance spoofs look just like the simulated ones: about 400 ms from placement to trade, the resting order 9× the trade. What sets them apart is that the pseudo-trader also rests plenty on the other side. The ratio of 3 was picked from AAPL's counts at 0, 2, 3 and 5; simulated spoofs pass up to 8. For spoofing, AMZN, GOOG and MSFT are held out. With ten traders their spoofing and layering alerts went from 21, 3 and 5 a day to 3, 1 and 0.

False alerts per trader-hour, all detectors (thresholds in shares → now):

| Stock (price) | 10 traders | 50 traders | 200 traders |
|---|---|---|---|
| AAPL ($582), in-sample for changes 4–5 | 19 → 0.26 | 0.041 → 0.028 | 0.003 → 0.002 |
| AMZN ($223) | 1.5 → 0.31 | 0.021 → 0.018 | 0.0003 → 0.0005 |
| GOOG ($569) | 1.1 → 0.10 | 0.007 → 0.006 | 0.0008 → 0.0005 |
| INTC ($27), in-sample | 17 → 0.09 | 0.93 → 0.001 | 0.22 → 0 |
| MSFT ($31) | 18 → 0.11 | 1.4 → 0.004 | 0.19 → 0.0005 |

What the real data shows now:
- **Price level no longer drives the counts.** With 200 traders every stock is at 0–0.002 false alerts per trader-hour (at most about 2 a day), against 0.19–0.22 for INTC and MSFT before. Front-running on INTC and MSFT with 50 or 200 traders went from 240–390 a day to 0–1.3 (MSFT held out). With ten traders the five stocks span 0.09–0.31, not 1.1–19, in no price order.
- **AAPL is still the noisiest at 50 traders**: 0.028, against 0.001–0.006 for INTC, MSFT and GOOG (GOOG costs about as much as AAPL). The rest is mostly spoofing, 6 a day: single orders of about 1,000 shares, ten times AAPL's usual order, cancelled within 2 seconds of a trade on the other side by a pseudo-trader that holds little on that side. Only real account data can show whether such coincidences happen with real owners.
- **Wash loops with ten traders** are down from 65–1,213 a day to 1–7. Loops between busy accounts no longer qualify, because they are a small share of each account's trading, and the self-trade artifact is gone.
- **What is left with ten traders**: front-running (5–10 a day on AMZN, INTC and MSFT), chance two-party loops (1–7) and spoofing on AAPL (11). When a few accounts share a whole stock's flow, their trades line up by chance.
- **Marking the close** never fired, but there are only five closes here.
- **The order-to-trade baseline** flags 0–80% of trader-windows, depending mostly on the stock rather than on behaviour.

Using this on real flow would still need real account attribution. Nasdaq's public feed attributes only some orders to a member firm, so that needs a venue's or broker's own records.

## Run it

```bash
pip install -r requirements.txt        # or: pip install -r requirements-dev.lock && pip install -e .
python -m pytest -q                    # 36 tests
python -m mktsurv bench                # single session + 200-session sweep
python -m mktsurv realdata --data DIR  # LOBSTER sample files, about 7 minutes
python -m mktsurv serve                # API + console at http://127.0.0.1:8015 (open, loopback only)
docker build -t market-manipulation-surveillance .   # production image; see docs/runbook.md
```

Endpoints:
- `POST /surveil`: your own events (up to 5,000) or the simulated session. Alerts raised on your events are stored and get IDs.
- `GET /alerts`, `GET /alerts/{id}`, `POST /alerts/{id}/review`: the alert queue. Escalate, dismiss or reopen an alert with a note; every review is kept with the reviewer's name.
- `GET /episodes`, `GET /episodes/{id}`, `GET /bench`: the simulator and benchmark.
- `GET /healthz`, `GET /readyz`, `GET /metrics`: liveness, readiness and Prometheus metrics.

Each caller gets an API key with a role: `feed` submits order flow, `viewer` reads, `analyst` submits, reads and reviews, `monitor` scrapes metrics. Make one with `python -m mktsurv.ops keygen` and list its hash in `MKTSURV_API_KEYS` or `MKTSURV_KEYS_FILE`; the console has a field for the key. Without keys the server runs open and refuses to bind a non-loopback address. With `MKTSURV_ENV=production` it refuses to start without hashed keys and a database. Settings, key rotation and backups are in [docs/runbook.md](docs/runbook.md); what the rules can and can't do is in [docs/model-card.md](docs/model-card.md).

## Limits

- **No labelled real cases.** Recall is measured only on simulated episodes. Real order flow (see Real data) gives false-alert counts, and only under made-up trader attribution.
- **Rules, not models.** Every threshold is a hand-set constant. Real surveillance would tune them per instrument and venue and review the alerts by hand.
- **One instrument, one venue.** There is no cross-venue or cross-product view; spoofing across related instruments and wash trades through other venues are invisible. Linkage between accounts is only the IP field; real wash-trade detection needs beneficial-ownership data.
- **Simplified matching engine.** There are no auctions, hidden or iceberg order types, self-trade prevention or tick-size checks. Every query rescans the book's levels, which is fine for thousands of events but nowhere near exchange volumes.
- **Front-running needs the victim's trades in the same feed** and only looks at one block at a time. It also needs the front-runner to be at least half of the volume just before the block, so someone front-running in small pieces inside heavy flow goes unseen.
- **Wash loops must dominate the members' trading.** Accounts that wash-trade with each other while also trading normally, so the loop is under half of their volume in the window, are not reported unless they share an IP.
- **Typical size comes from the data being checked.** In a quiet window a manipulator's own large orders raise it, which raises the thresholds they are measured against.
- **Thin case management.** Alerts and reviews are stored, but there is no assignment, SLA tracking or tamper-evident history of reviews. TLS is left to the ingress.

## Production readiness

**In place** (see [docs/runbook.md](docs/runbook.md), [docs/threat-model.md](docs/threat-model.md), [docs/model-card.md](docs/model-card.md)):
- Pinned dependencies (`requirements.lock`), `pyproject.toml`, a container image that runs as a non-root user with a health check, and CI (ruff, bandit, pip-audit, tests with coverage).
- Production mode that refuses to start without hashed per-client API keys and a database, and no public listening without keys. There are no default secrets.
- Roles on keys, per-client rate limits, a body cap, security headers, CORS only for listed origins, and errors that don't echo input or internals.
- Alerts, the runs that raised them and analyst reviews in SQLite or Postgres, with numbered migrations.
- JSON logs with request IDs, `/healthz`, `/readyz`, and Prometheus metrics including alerts by pattern.

**Still needed before it watches a real market**, none of which code can supply:
- An independent security review and penetration test.
- Validation on a venue's or broker's own order flow with real account attribution. Recall on real data is unknown, and the real-data false-alert rates above rest on random pseudo-traders.
- Sign-off from compliance and the regulator (SEBI's surveillance requirements for the venue or broker; RBI where the bank's treasury desk is the user; DPDP Act 2023 for personal data in order records).
- Real integrations: the venue's or OMS's order feed, beneficial-ownership data for account linkage, and the case management system analysts already use.
- Keys and the database in managed infrastructure (a KMS or HSM for key material, encrypted storage, retention rules), and an on-call team with SLOs for alert review.
