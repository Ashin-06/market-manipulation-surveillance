# Model card: mktsurv detection rules

The "model" here is a set of hand-written threshold rules, not a trained model. This card says
what they are for, how they were measured and where they fail. Numbers come from the README and
`results/real_data.json`.

## What it does

Replays level-3 order events through a price-time-priority book and runs four rule families over
order lifecycles and fills:

| Rule | Fires when (summary; full definitions in the README) |
|---|---|
| Spoofing / layering | ≥ 5× typical order size resting on one side, book imbalance toward it > 0.55, a trade on the other side, > 80% of the resting size cancelled within 2 s; the trader rests ≥ 3× as much on the spoof side as on the trade side. Two or more levels = layering. |
| Wash trading | self-trades, or 2- and 3-party loops within 60 s with net change < 5% of gross, *and* evidence of prearrangement (shared IP; or repeated or matched legs that are ≥ half of each member's trading) |
| Marking the close | one trader's aggressive orders > 40% of the last 120 s volume and the close > 35 bps from the prior 15-minute VWAP |
| Front-running | a position built in the 15 s before a block (≥ 3× typical fill) from someone else, ≥ 60% unwound into the block at ≥ 8 bps; position ≥ half a typical fill and ≥ half the volume in those 15 s |

Sizes are multiples of the instrument's typical size (volume-weighted median of order or fill sizes
in the data being checked), not shares.

## Intended use

To raise alerts for a human surveillance analyst on one instrument's order flow, with account
attribution supplied by the venue or broker. It is not meant to take action on its own, to
prove manipulation, or to run without someone reviewing its alerts.

## Evaluation

**Simulator** (`python -m mktsurv bench`): 200 sessions, each episode injected with p = 0.6,
scored per trader.

| | Precision | Recall | False positives | Hard negatives flagged |
|---|---|---|---|---|
| mktsurv | 100% | 100% (952/952) | 0 | 0% |
| OTR > 15 / cancel > 85% baseline | 0% | 0% | 478 | 59.8% |

1,000 further sessions on seeds never used for tuning (1000–1999): 4,862/4,862 episodes found,
no false positives.

**Caveat:** every session reuses the same six episode templates, shifted and rescaled, and the
thresholds were set with those templates in view. The 100% means the rules survive the
simulator's variation, not that they catch strategies they weren't written for.

**Real order flow** (LOBSTER Nasdaq samples, AAPL, AMZN, GOOG, INTC, MSFT, 21 June 2012). No
manipulation labels exist, and the data has no trader IDs: orders were assigned to N
pseudo-traders at random (mean of 3 attributions), so every alert counts as false. False alerts
per trader-hour:

| Stock | 10 traders | 50 traders | 200 traders |
|---|---|---|---|
| AAPL (in-sample for wash-leg pricing and the one-sided spoofing rule) | 0.26 | 0.028 | 0.002 |
| AMZN | 0.31 | 0.018 | 0.0005 |
| GOOG | 0.10 | 0.006 | 0.0005 |
| INTC (in-sample for the size and share rules) | 0.09 | 0.001 | 0 |
| MSFT | 0.11 | 0.004 | 0.0005 |

What is in-sample: changes 1–3 (sizes as multiples of typical size, front-running share of
volume, wash loops as share of trading) were tuned on the simulator and INTC; the matched-legs
price rule came after seeing every stock's results, so no stock is a clean hold-out for wash
trading; the one-sided spoofing ratio of 3 was tuned on AAPL, with AMZN, GOOG and MSFT held out.
Recall on real data is unknown.

## Known failure modes

- Thresholds are public and fixed; a manipulator who knows them can stay under them.
- Random pseudo-trader attribution is not real ownership: real firms' quoting clusters, which
  could give more or fewer alerts. At 50 traders AAPL still has about 6 chance spoofing alerts a
  day from single ~1,000-share orders.
- One instrument and one venue; no cross-product or cross-venue view; account linkage only by IP.
- Typical size is computed from the data being checked, so a manipulator's own large orders in a
  quiet window raise the thresholds.
- Wash loops that are under half of the members' trading are missed unless the accounts share
  an IP; front-running in small pieces inside heavy flow is missed.
- Marking the close never fired on real data, but there were only five closes.

## Before relying on it

Run it on the venue's or broker's own order flow with real account attribution, review a sample
of alerts and of non-alerted activity by hand, and tune thresholds per instrument with the result.
