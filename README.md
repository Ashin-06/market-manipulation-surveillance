# Market Manipulation Surveillance

Detects spoofing, layering, wash trading, marking the close and front-running from order-book events. It replays every order add, cancel, change and trade through a matching engine and runs rule-based detectors over each order's life and fills.

The usual surveillance baseline, an order-to-trade ratio rule, flags busy market makers and execution algorithms while missing manipulators who keep their ratios normal. Spoofing only shows up when an order's life is read against the state of the book.

## Features

- A price-time-priority order book that tracks each order's fills, cancels, time to cancel and the book imbalance when it was placed.
- Detectors for spoofing and layering, wash trading (self-trades and 2–3 party loops with signs of prearrangement), marking the close and front-running.
- Thresholds scale with each stock's typical trade size rather than fixed share counts.
- A simulator with legitimate high-cancel algorithms as hard negatives and six injected manipulation patterns.
- A stored alert queue where analysts escalate, dismiss or reopen alerts with notes.

The detector rules and the simulator are described in [docs/design.md](docs/design.md).

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q                    # 36 tests
python -m mktsurv bench                # one session and a 200-session sweep
python -m mktsurv realdata --data DIR  # LOBSTER sample files, about 7 minutes
python -m mktsurv serve                # API and console on http://127.0.0.1:8015
```

To build the container image: `docker build -t market-manipulation-surveillance .` (see [docs/runbook.md](docs/runbook.md)).

## API

| Endpoint | Purpose |
|---|---|
| `POST /surveil` | check your own events (up to 5,000) or the simulated session; alerts on your events are stored |
| `GET /alerts`, `/alerts/{id}` | the alert queue |
| `POST /alerts/{id}/review` | escalate, dismiss or reopen an alert with a note |
| `GET /episodes`, `/episodes/{id}`, `/bench` | the simulator and benchmark |
| `GET /healthz`, `/readyz`, `/metrics` | health, readiness and Prometheus metrics |

API keys carry a role: `feed` submits order flow, `viewer` reads, `analyst` submits, reads and reviews, `monitor` reads metrics. Create one with `python -m mktsurv.ops keygen`.

## Results

| Test | Result |
|---|---|
| 200 simulated sessions | every injected episode caught (952 of 952), no false alerts; the ratio rule caught none and flagged 60% of legitimate algorithms |
| Real Nasdaq order flow (LOBSTER, five stocks, one day) with 200 pseudo-traders | 0–0.002 false alerts per trader-hour, down from up to 0.22 with share-based thresholds |
| Same, with only 10 pseudo-traders | 0.09–0.31 false alerts per trader-hour, down from 1.1–19 |

The simulated sessions reuse six episode templates, so they show robustness to timing and size rather than to new strategies. The real data has no labels or account IDs, so orders were assigned to pseudo-traders at random and only false alerts can be counted. Some thresholds were tuned on two of the five stocks. Details are in [docs/evaluation.md](docs/evaluation.md).

## Configuration

| Variable | Purpose |
|---|---|
| `MKTSURV_ENV=production` | refuse to start without hashed API keys and a database |
| `MKTSURV_API_KEYS` / `MKTSURV_KEYS_FILE` | hashed API keys with roles |
| `MKTSURV_DATABASE_URL` | `sqlite:///...` or `postgresql://...` |

## Limitations

- Recall is only measured on simulated episodes; there are no labelled real cases.
- Every threshold is a hand-set constant. Real surveillance would tune them per instrument and venue.
- One instrument and one venue: manipulation across related instruments or other venues is invisible, and accounts are only linked by IP address.
- The matching engine has no auctions, hidden orders or self-trade prevention, and is sized for thousands of events, not exchange volumes.
- Front-running in small pieces inside heavy flow, and wash trading mixed with normal trading, can go unseen.
- Using it on real flow needs real account attribution from a venue's or broker's records.
- The code has not had an independent security review.

See [SECURITY.md](SECURITY.md), [docs/threat-model.md](docs/threat-model.md) and [docs/model-card.md](docs/model-card.md).
