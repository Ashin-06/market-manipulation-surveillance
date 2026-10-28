# Evaluation

## Simulated sessions

`python -m mktsurv bench` scores one session (seed 42) and then 200 sessions with each episode injected with probability 0.6. Scoring is per trader.

| 200 sessions | Precision | Recall | False positives | Hard negatives flagged |
|---|---|---|---|---|
| mktsurv | 100% | 100% (952/952) | 0 | 0% |
| Order-to-trade ratio rule | 0% | 0% | 478 | 59.8% |

Every injected episode was detected: spoofing 122/122, layering 112/112, self-trades 121/121, rings 122/122, front-running 114/114 and closing ramps 117/117. Sweeps with every episode always included and with none also gave no false positives and no misses, as did 1,000 sessions on seeds never used for tuning (4,862/4,862).

Every session reuses the same six episode templates, shifted and rescaled, and the thresholds were set with them in view. So this shows the detectors cope with changes in timing, size and price level, not that they catch strategies they weren't written for.

## Real Nasdaq order flow

`python -m mktsurv realdata --data DIR` runs the detectors over LOBSTER's free Nasdaq samples: every add, cancel and execution within the top 10 price levels of AAPL, AMZN, GOOG, INTC and MSFT on 21 June 2012, 9:30 to 16:00. That is 74,000–331,000 orders and 11,000–35,000 fills per stock. Results go to `results/real_data.json`.

The data has no manipulation labels and no trader, account or IP fields, so this measures false alerts only:
- Orders and fills are built directly from the messages, since Nasdaq already matched them.
- Each order is given to one of N pseudo-traders at random, and each burst of executions with the same timestamp and side becomes one order from another pseudo-trader that owns none of the orders it hits. Any alert is therefore a coincidence of real order flow and counts as false. Results are averaged over 3 random assignments.
- The detectors run on 26 fifteen-minute windows; marking the close runs on the last one.

The first run used share-based thresholds (a 1,200-share block, a 200-share front-running position, 500 shares of spoof orders, 500-share wash loops). On INTC and MSFT that is routine flow: front-running fired 240–390 times a day even with 200 traders, and with ten traders chance wash loops came to 65–1,213 a day on every stock. Five changes followed:

1. Every size threshold became a multiple of the stock's typical size.
2. A front-running position must be at least half of the volume in the 15 seconds before the block.
3. A wash loop must be at least half of each member's trading in the window.
4. Matched wash-loop legs must also trade at one price.
5. A spoofer must be one-sided.

Changes 1–3 were tuned on the simulator and on INTC, so INTC is in-sample for them. Change 4 came from looking at AAPL loops after the first results on every stock, so no stock is a clean hold-out for the wash rule. Change 5 was tuned on AAPL, leaving AMZN, GOOG and MSFT held out for spoofing; with ten traders their spoofing alerts went from 21, 3 and 5 a day to 3, 1 and 0.

False alerts per trader-hour, all detectors (share-based thresholds → now):

| Stock (price) | 10 traders | 50 traders | 200 traders |
|---|---|---|---|
| AAPL ($582), in-sample for changes 4–5 | 19 → 0.26 | 0.041 → 0.028 | 0.003 → 0.002 |
| AMZN ($223) | 1.5 → 0.31 | 0.021 → 0.018 | 0.0003 → 0.0005 |
| GOOG ($569) | 1.1 → 0.10 | 0.007 → 0.006 | 0.0008 → 0.0005 |
| INTC ($27), in-sample | 17 → 0.09 | 0.93 → 0.001 | 0.22 → 0 |
| MSFT ($31) | 18 → 0.11 | 1.4 → 0.004 | 0.19 → 0.0005 |

- Price level no longer drives the counts. With 200 traders every stock is at 0–0.002 false alerts per trader-hour, at most about 2 a day.
- AAPL is still the noisiest at 50 traders, mostly from single orders of about 1,000 shares cancelled within 2 seconds of a trade on the other side. Only real account data can show whether such coincidences happen with real owners.
- With ten traders, wash loops are down from 65–1,213 a day to 1–7. What remains with so few accounts is front-running (5–10 a day on AMZN, INTC and MSFT), chance two-party loops and spoofing on AAPL, because when a few accounts share a whole stock's flow their trades line up by chance.
- Marking the close never fired, but there are only five closes in the data.
- The order-to-trade ratio rule flags 0–80% of trader-windows, depending mostly on the stock rather than on behaviour.
