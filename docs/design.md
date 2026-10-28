# Design

## Order book

`lob.py` is a single-instrument book with price-time priority. It tracks each order's fills, cancels, time to cancel and distance from the best price, plus the book imbalance when the order was placed, and reports the mid price, micro-price and spread. Events with unknown types or sides, non-finite or negative prices, non-positive quantities or reused order IDs are rejected.

## Typical size

Size thresholds are not in shares but in multiples of the instrument's typical size: the volume-weighted median of the order or fill sizes in the data being checked, so half of all volume goes through pieces that size or smaller. A plain median would not work, because most Nasdaq fills are 100 shares whether the stock costs $27 or $580; what differs is how large the big ones get. The typical fill is about 650 shares on INTC and MSFT and 100 on AAPL, AMZN and GOOG.

## Detectors

`detectors.py` and `vpin.py`. All detectors are threshold rules with hand-set parameters.

- Spoofing and layering: a trader rests at least 5 times the typical order size on one side while the book leans that way (imbalance above 0.55), trades on the other side, then cancels more than 80% of the resting orders within 2 seconds of that trade. The trader must also be one-sided: the resting volume is at least 3 times their own live orders on the trade's side. A market maker also pulls a large quote after a fill, but it rests about as much on both sides. Two or more price levels count as layering.
- Wash trading: self-trades (same trader or account on both sides) and 2- or 3-party loops within 60 seconds whose net position change is under 5% of gross. A loop is reported only with evidence of prearrangement: a shared IP, or else every leg traded at least twice or all legs the same size at one price, and in both cases the loop making up at least half of each member's trading in the window.
- Marking the close: one trader's aggressive orders make up over 40% of volume in the last 120 seconds and move the close more than 35 basis points from the preceding 15-minute VWAP.
- Front-running: a trader builds a position in the 15 seconds before someone else's block trade (at least 3 times the typical fill), then unwinds at least 60% of it into the block at an edge of 8 basis points or more. The position must be at least half the typical fill and at least half of all volume in those 15 seconds. VPIN is reported with the alert but is not part of the rule.

The baseline for comparison is the classic order-to-trade ratio rule (ratio above 15 or cancel rate above 85%).

## Simulator

`simulate.py` produces a 15-minute session with two market makers, six retail traders and two legitimate institutional algorithms with high cancel rates (a TWAP and an iceberg), which serve as hard negatives. Up to six manipulation episodes are injected, one per pattern: spoofing, layering, a self-trade, a three-account ring, front-running and a closing ramp. Each seed changes the price level, timing, order sizes, number of slices and cancel latency, and each episode can be left out at random. Detectors never see trader names or tags; those are only used for scoring.
