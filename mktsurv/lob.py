"""
Deterministic Price-Time Priority Level-3 Limit Order Book (LOB) Matching Engine.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Tuple


def typical_size(sizes: Iterable[int]) -> float:
    """
    Volume-weighted median size: half of all volume goes through pieces of this size or smaller.

    The detectors express size thresholds as multiples of this, so they follow each instrument's
    own trading instead of fixed share counts. A plain median would not do: most Nasdaq fills are
    100 shares whether the stock costs $27 or $580; what differs is how large the big ones get.
    """
    s = sorted(sizes)
    half, run = sum(s) / 2.0, 0
    for q in s:
        run += q
        if run >= half:
            return float(q)
    return 0.0


@dataclass
class OrderEvent:
    """Level-3 order event entering the matching engine."""

    event_type: str  # "ADD", "CANCEL", "MODIFY", "MARKET"
    order_id: str
    trader_id: str
    account_id: str
    ip_address: str
    side: str  # "BUY" or "SELL"
    price: float
    qty: int
    timestamp_ms: int
    tag: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RestingOrder:
    """Resting limit order on the book queue."""

    order_id: str
    trader_id: str
    account_id: str
    ip_address: str
    side: str
    price: float
    initial_qty: int
    remaining_qty: int
    timestamp_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TradeFill:
    """Matched execution record emitted by the LOB engine."""

    fill_id: str
    timestamp_ms: int
    price: float
    qty: int
    aggressor_side: str  # "BUY" or "SELL"
    aggressor_order_id: str
    aggressor_trader_id: str
    aggressor_account_id: str
    aggressor_ip: str
    passive_order_id: str
    passive_trader_id: str
    passive_account_id: str
    passive_ip: str
    buyer_trader_id: str
    buyer_account_id: str
    buyer_ip: str
    seller_trader_id: str
    seller_account_id: str
    seller_ip: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrderLifecycle:
    """Full lifecycle metrics for every order submitted to the LOB."""

    order_id: str
    trader_id: str
    account_id: str
    ip_address: str
    side: str
    price: float
    initial_qty: int
    filled_qty: int = 0
    canceled_qty: int = 0
    remaining_qty: int = 0
    placed_ts_ms: int = 0
    first_fill_ts_ms: Optional[int] = None
    canceled_ts_ms: Optional[int] = None
    last_event_ts_ms: int = 0
    distance_from_touch_bps: float = 0.0
    book_imbalance_at_placement: float = 0.0
    status: str = "OPEN"  # "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED"

    @property
    def time_to_cancel_ms(self) -> Optional[int]:
        if self.canceled_ts_ms is None:
            return None
        return max(0, self.canceled_ts_ms - self.placed_ts_ms)

    @property
    def fill_ratio(self) -> float:
        if self.initial_qty <= 0:
            return 0.0
        return self.filled_qty / float(self.initial_qty)

    @property
    def cancel_ratio(self) -> float:
        if self.initial_qty <= 0:
            return 0.0
        return self.canceled_qty / float(self.initial_qty)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time_to_cancel_ms"] = self.time_to_cancel_ms
        d["fill_ratio"] = round(self.fill_ratio, 6)
        d["cancel_ratio"] = round(self.cancel_ratio, 6)
        return d


@dataclass
class BookSnapshot:
    """L2/L3 top-of-book and depth snapshot at a point in time."""

    timestamp_ms: int
    best_bid: Optional[float]
    best_ask: Optional[float]
    best_bid_qty: int
    best_ask_qty: int
    mid_price: Optional[float]
    micro_price: Optional[float]
    spread_bps: Optional[float]
    obi_top1: float
    obi_top5: float
    bids: List[Tuple[float, int]] = field(default_factory=list)
    asks: List[Tuple[float, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class LimitOrderBook:
    """
    Deterministic price-time priority Level-3 Limit Order Book (LOB).

    Supports ADD, CANCEL, MODIFY, and MARKET order events.
    Tracks resting order queues, matched TradeFills, OrderLifecycle metrics,
    mid-price, micro-price, and Order Book Imbalance (OBI).
    """

    def __init__(self, symbol: str = "ACME", tick_size: float = 0.01) -> None:
        self.symbol = symbol
        self.tick_size = tick_size
        self.bids: Dict[float, Deque[RestingOrder]] = {}
        self.asks: Dict[float, Deque[RestingOrder]] = {}
        self.order_map: Dict[str, RestingOrder] = {}
        self.lifecycles: Dict[str, OrderLifecycle] = {}
        self.fills: List[TradeFill] = []
        self.snapshots: List[BookSnapshot] = []
        self.events: List[OrderEvent] = []
        self._fill_seq: int = 0

    def _round_price(self, price: float) -> float:
        return round(price, 4)

    def _clean_empty_levels(self) -> None:
        empty_bids = [p for p, q in self.bids.items() if not q or sum(o.remaining_qty for o in q) <= 0]
        for p in empty_bids:
            del self.bids[p]
        empty_asks = [p for p, q in self.asks.items() if not q or sum(o.remaining_qty for o in q) <= 0]
        for p in empty_asks:
            del self.asks[p]

    def best_bid(self) -> Optional[float]:
        self._clean_empty_levels()
        return max(self.bids.keys()) if self.bids else None

    def best_ask(self) -> Optional[float]:
        self._clean_empty_levels()
        return min(self.asks.keys()) if self.asks else None

    def best_bid_qty(self) -> int:
        bb = self.best_bid()
        if bb is None:
            return 0
        return sum(o.remaining_qty for o in self.bids[bb])

    def best_ask_qty(self) -> int:
        ba = self.best_ask()
        if ba is None:
            return 0
        return sum(o.remaining_qty for o in self.asks[ba])

    def mid_price(self) -> Optional[float]:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is not None and ba is not None:
            return 0.5 * (bb + ba)
        return bb if bb is not None else ba

    def micro_price(self) -> Optional[float]:
        """
        Volume-weighted best bid/ask micro-price:
        MicroPrice = (V_bid * P_ask + V_ask * P_bid) / (V_bid + V_ask)
        """
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return self.mid_price()
        v_bid = self.best_bid_qty()
        v_ask = self.best_ask_qty()
        total = v_bid + v_ask
        if total <= 0:
            return 0.5 * (bb + ba)
        return (v_bid * ba + v_ask * bb) / float(total)

    def spread_bps(self) -> Optional[float]:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return None
        mid = 0.5 * (bb + ba)
        if mid <= 0:
            return 0.0
        return ((ba - bb) / mid) * 10000.0

    def depth_levels(self, side: str, levels: int = 5) -> List[Tuple[float, int]]:
        self._clean_empty_levels()
        if side.upper() == "BUY":
            prices = sorted(self.bids.keys(), reverse=True)[:levels]
            return [(p, sum(o.remaining_qty for o in self.bids[p])) for p in prices]
        else:
            prices = sorted(self.asks.keys())[:levels]
            return [(p, sum(o.remaining_qty for o in self.asks[p])) for p in prices]

    def order_book_imbalance(self, levels: int = 5) -> float:
        """
        Computes Order Book Imbalance (OBI) over the top N price levels:
        OBI = (V_bid - V_ask) / (V_bid + V_ask) in [-1.0, +1.0].
        """
        bid_lvls = self.depth_levels("BUY", levels=levels)
        ask_lvls = self.depth_levels("SELL", levels=levels)
        v_bid = sum(q for _, q in bid_lvls)
        v_ask = sum(q for _, q in ask_lvls)
        denom = v_bid + v_ask
        if denom <= 0:
            return 0.0
        return (v_bid - v_ask) / float(denom)

    def snapshot(self, timestamp_ms: int = 0, depth: int = 5) -> BookSnapshot:
        bb = self.best_bid()
        ba = self.best_ask()
        return BookSnapshot(
            timestamp_ms=timestamp_ms,
            best_bid=bb,
            best_ask=ba,
            best_bid_qty=self.best_bid_qty(),
            best_ask_qty=self.best_ask_qty(),
            mid_price=self.mid_price(),
            micro_price=self.micro_price(),
            spread_bps=self.spread_bps(),
            obi_top1=self.order_book_imbalance(levels=1),
            obi_top5=self.order_book_imbalance(levels=5),
            bids=self.depth_levels("BUY", levels=depth),
            asks=self.depth_levels("SELL", levels=depth),
        )

    def _compute_distance_from_touch_bps(self, side: str, price: float) -> float:
        if price <= 0:
            return 0.0
        if side == "BUY":
            bb = self.best_bid()
            ref = bb if bb is not None else self.mid_price()
            if ref is None or ref <= 0:
                return 0.0
            return max(0.0, ((ref - price) / ref) * 10000.0)
        else:
            ba = self.best_ask()
            ref = ba if ba is not None else self.mid_price()
            if ref is None or ref <= 0:
                return 0.0
            return max(0.0, ((price - ref) / ref) * 10000.0)

    def _match_incoming(
        self,
        event: OrderEvent,
        limit_price: Optional[float],
        qty_to_match: int,
    ) -> Tuple[int, List[TradeFill]]:
        """
        Matches incoming order quantity against opposite book side in price-time priority.
        Returns (remaining_unfilled_qty, emitted_fills).
        """
        remaining = qty_to_match
        emitted: List[TradeFill] = []
        side = event.side.upper()
        aggressor_lc = self.lifecycles[event.order_id]

        while remaining > 0:
            if side == "BUY":
                best_opp = self.best_ask()
                if best_opp is None:
                    break
                if limit_price is not None and best_opp > limit_price + 1e-9:
                    break
                queue = self.asks[best_opp]
            else:
                best_opp = self.best_bid()
                if best_opp is None:
                    break
                if limit_price is not None and best_opp < limit_price - 1e-9:
                    break
                queue = self.bids[best_opp]

            while queue and remaining > 0:
                passive = queue[0]
                trade_qty = min(remaining, passive.remaining_qty)
                trade_price = passive.price

                remaining -= trade_qty
                passive.remaining_qty -= trade_qty

                # Update passive lifecycle
                passive_lc = self.lifecycles.get(passive.order_id)
                if passive_lc is not None:
                    passive_lc.filled_qty += trade_qty
                    passive_lc.remaining_qty = passive.remaining_qty
                    passive_lc.last_event_ts_ms = event.timestamp_ms
                    if passive_lc.first_fill_ts_ms is None:
                        passive_lc.first_fill_ts_ms = event.timestamp_ms
                    if passive.remaining_qty == 0:
                        passive_lc.status = "FILLED"
                    else:
                        passive_lc.status = "PARTIALLY_FILLED"

                # Update aggressor lifecycle
                aggressor_lc.filled_qty += trade_qty
                aggressor_lc.remaining_qty = remaining
                aggressor_lc.last_event_ts_ms = event.timestamp_ms
                if aggressor_lc.first_fill_ts_ms is None:
                    aggressor_lc.first_fill_ts_ms = event.timestamp_ms
                if remaining == 0:
                    aggressor_lc.status = "FILLED"
                else:
                    aggressor_lc.status = "PARTIALLY_FILLED"

                self._fill_seq += 1
                if side == "BUY":
                    buyer_trader, buyer_acct, buyer_ip = (
                        event.trader_id,
                        event.account_id,
                        event.ip_address,
                    )
                    seller_trader, seller_acct, seller_ip = (
                        passive.trader_id,
                        passive.account_id,
                        passive.ip_address,
                    )
                else:
                    buyer_trader, buyer_acct, buyer_ip = (
                        passive.trader_id,
                        passive.account_id,
                        passive.ip_address,
                    )
                    seller_trader, seller_acct, seller_ip = (
                        event.trader_id,
                        event.account_id,
                        event.ip_address,
                    )

                fill = TradeFill(
                    fill_id=f"FILL-{self._fill_seq:06d}",
                    timestamp_ms=event.timestamp_ms,
                    price=trade_price,
                    qty=trade_qty,
                    aggressor_side=side,
                    aggressor_order_id=event.order_id,
                    aggressor_trader_id=event.trader_id,
                    aggressor_account_id=event.account_id,
                    aggressor_ip=event.ip_address,
                    passive_order_id=passive.order_id,
                    passive_trader_id=passive.trader_id,
                    passive_account_id=passive.account_id,
                    passive_ip=passive.ip_address,
                    buyer_trader_id=buyer_trader,
                    buyer_account_id=buyer_acct,
                    buyer_ip=buyer_ip,
                    seller_trader_id=seller_trader,
                    seller_account_id=seller_acct,
                    seller_ip=seller_ip,
                )
                self.fills.append(fill)
                emitted.append(fill)

                if passive.remaining_qty == 0:
                    queue.popleft()
                    self.order_map.pop(passive.order_id, None)

            self._clean_empty_levels()

        return remaining, emitted

    def _insert_resting(self, resting: RestingOrder) -> None:
        price = self._round_price(resting.price)
        resting.price = price
        book_side = self.bids if resting.side == "BUY" else self.asks
        if price not in book_side:
            book_side[price] = deque()
        book_side[price].append(resting)
        self.order_map[resting.order_id] = resting

    def _validate(self, event: OrderEvent) -> None:
        """
        Reject events the book would otherwise mis-process: a NaN limit price compares false
        against every level and so trades like a market order, and a reused order ID leaves
        the earlier order orphaned on the book where it can never be cancelled.
        """
        etype = event.event_type.upper()
        if etype not in ("ADD", "CANCEL", "MODIFY", "MARKET"):
            raise ValueError(f"Unknown event_type {event.event_type!r} for order {event.order_id}")
        if event.side.upper() not in ("BUY", "SELL"):
            raise ValueError(f"Unknown side {event.side!r} for order {event.order_id}")
        if not math.isfinite(event.price) or event.price < 0:
            raise ValueError(f"Price must be a finite non-negative number (order {event.order_id})")
        if event.qty < 0:
            raise ValueError(f"Quantity cannot be negative (order {event.order_id})")
        if etype in ("ADD", "MARKET"):
            if event.qty == 0:
                raise ValueError(f"{etype} needs a positive quantity (order {event.order_id})")
            if etype == "ADD" and event.price <= 0:
                raise ValueError(f"ADD needs a positive limit price (order {event.order_id})")
            if event.order_id in self.lifecycles:
                raise ValueError(f"Duplicate order_id {event.order_id}")

    def process_event(
        self, event: OrderEvent, record_snapshot: bool = False
    ) -> List[TradeFill]:
        """Process a single Level-3 OrderEvent and return any resulting TradeFills."""
        self._validate(event)
        self.events.append(event)
        etype = event.event_type.upper()
        side = event.side.upper()
        price = self._round_price(event.price)
        emitted: List[TradeFill] = []

        if etype == "ADD":
            dist_bps = self._compute_distance_from_touch_bps(side, price)
            lc = OrderLifecycle(
                order_id=event.order_id,
                trader_id=event.trader_id,
                account_id=event.account_id,
                ip_address=event.ip_address,
                side=side,
                price=price,
                initial_qty=event.qty,
                filled_qty=0,
                canceled_qty=0,
                remaining_qty=event.qty,
                placed_ts_ms=event.timestamp_ms,
                last_event_ts_ms=event.timestamp_ms,
                distance_from_touch_bps=dist_bps,
                status="OPEN",
            )
            self.lifecycles[event.order_id] = lc

            remaining, emitted = self._match_incoming(
                event=event, limit_price=price, qty_to_match=event.qty
            )
            if remaining > 0:
                resting = RestingOrder(
                    order_id=event.order_id,
                    trader_id=event.trader_id,
                    account_id=event.account_id,
                    ip_address=event.ip_address,
                    side=side,
                    price=price,
                    initial_qty=event.qty,
                    remaining_qty=remaining,
                    timestamp_ms=event.timestamp_ms,
                )
                self._insert_resting(resting)
            lc.book_imbalance_at_placement = self.order_book_imbalance(levels=5)

        elif etype == "MARKET":
            lc = OrderLifecycle(
                order_id=event.order_id,
                trader_id=event.trader_id,
                account_id=event.account_id,
                ip_address=event.ip_address,
                side=side,
                price=price,
                initial_qty=event.qty,
                filled_qty=0,
                canceled_qty=0,
                remaining_qty=event.qty,
                placed_ts_ms=event.timestamp_ms,
                last_event_ts_ms=event.timestamp_ms,
                distance_from_touch_bps=0.0,
                status="OPEN",
            )
            self.lifecycles[event.order_id] = lc
            remaining, emitted = self._match_incoming(
                event=event, limit_price=None, qty_to_match=event.qty
            )
            if remaining > 0:
                # Any unfilled market order portion expires (IOC semantics)
                lc.canceled_qty += remaining
                lc.remaining_qty = 0
                lc.canceled_ts_ms = event.timestamp_ms
                if lc.filled_qty == 0:
                    lc.status = "CANCELED"
            lc.book_imbalance_at_placement = self.order_book_imbalance(levels=5)

        elif etype == "CANCEL":
            resting = self.order_map.get(event.order_id)
            if resting is not None:
                cancel_amt = (
                    resting.remaining_qty
                    if event.qty <= 0 or event.qty >= resting.remaining_qty
                    else event.qty
                )
                resting.remaining_qty -= cancel_amt
                lc = self.lifecycles.get(event.order_id)
                if lc is not None:
                    lc.canceled_qty += cancel_amt
                    lc.remaining_qty = resting.remaining_qty
                    lc.canceled_ts_ms = event.timestamp_ms
                    lc.last_event_ts_ms = event.timestamp_ms
                    if resting.remaining_qty == 0:
                        lc.status = "CANCELED" if lc.filled_qty == 0 else "PARTIALLY_FILLED"

                if resting.remaining_qty <= 0:
                    book_side = self.bids if resting.side == "BUY" else self.asks
                    q = book_side.get(resting.price)
                    if q is not None:
                        try:
                            q.remove(resting)
                        except ValueError:
                            pass
                    self.order_map.pop(event.order_id, None)
                    self._clean_empty_levels()

        elif etype == "MODIFY":
            resting = self.order_map.get(event.order_id)
            if resting is not None:
                lc = self.lifecycles.get(event.order_id)
                new_price = price if price > 0 else resting.price
                new_qty = event.qty

                if new_qty <= 0:
                    # Treat as full cancel
                    cancel_event = OrderEvent(
                        event_type="CANCEL",
                        order_id=event.order_id,
                        trader_id=event.trader_id,
                        account_id=event.account_id,
                        ip_address=event.ip_address,
                        side=resting.side,
                        price=resting.price,
                        qty=resting.remaining_qty,
                        timestamp_ms=event.timestamp_ms,
                    )
                    return self.process_event(cancel_event, record_snapshot=record_snapshot)

                if abs(new_price - resting.price) < 1e-9 and new_qty <= resting.remaining_qty:
                    # Size reduction at same price retains time priority
                    diff = resting.remaining_qty - new_qty
                    resting.remaining_qty = new_qty
                    if lc is not None:
                        lc.canceled_qty += diff
                        lc.remaining_qty = new_qty
                        lc.last_event_ts_ms = event.timestamp_ms
                else:
                    # Price change or size increase loses time priority
                    book_side = self.bids if resting.side == "BUY" else self.asks
                    q = book_side.get(resting.price)
                    if q is not None:
                        try:
                            q.remove(resting)
                        except ValueError:
                            pass
                    self.order_map.pop(event.order_id, None)
                    self._clean_empty_levels()

                    if lc is not None:
                        if new_qty > resting.remaining_qty:
                            lc.initial_qty += new_qty - resting.remaining_qty
                        elif new_qty < resting.remaining_qty:
                            lc.canceled_qty += resting.remaining_qty - new_qty
                        lc.price = new_price
                        lc.remaining_qty = new_qty
                        lc.last_event_ts_ms = event.timestamp_ms

                    mod_event = OrderEvent(
                        event_type="ADD",
                        order_id=event.order_id,
                        trader_id=resting.trader_id,
                        account_id=resting.account_id,
                        ip_address=resting.ip_address,
                        side=resting.side,
                        price=new_price,
                        qty=new_qty,
                        timestamp_ms=event.timestamp_ms,
                    )
                    remaining, emitted = self._match_incoming(
                        event=mod_event, limit_price=new_price, qty_to_match=new_qty
                    )
                    if remaining > 0:
                        resting.price = new_price
                        resting.remaining_qty = remaining
                        resting.timestamp_ms = event.timestamp_ms
                        self._insert_resting(resting)

        if record_snapshot:
            self.snapshots.append(self.snapshot(timestamp_ms=event.timestamp_ms))

        return emitted

    def process_stream(
        self, events: Iterable[OrderEvent], snapshot_every: int = 1
    ) -> List[TradeFill]:
        """Process an iterable of OrderEvents and optionally record periodic snapshots."""
        all_fills: List[TradeFill] = []
        for idx, ev in enumerate(events):
            rec = snapshot_every > 0 and ((idx + 1) % snapshot_every == 0)
            fills = self.process_event(ev, record_snapshot=rec)
            all_fills.extend(fills)
        return all_fills
