"""
Real order-management layer: takes the quotes strategy.py already computes
and decides what to actually do about them against Kalshi — place, amend,
or cancel a REAL resting order — subject to two things the paper simulation
never needed:

1. Throttling. The simulation recomputes a quote on every book tick; doing
   that against the real API would blow through Basic-tier write limits
   (100 tokens/sec, ~10 orders/sec sustained) almost immediately across
   multiple markets. Real updates are throttled by both a minimum interval
   and a minimum price-change threshold.
2. A dry-run gate (`live=False` by default everywhere). With it off, this
   module decides every real action it *would* take and logs it, but never
   calls kalshi_orders — nothing reaches Kalshi. Flip `live=True` only when
   ready to place real orders.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import kalshi_orders
import strategy


@dataclass
class RestingOrder:
    order_id: str
    price_cents: int
    count: int


@dataclass
class TickerOmsState:
    bid: RestingOrder | None = None
    ask: RestingOrder | None = None
    last_update_ts: float = 0.0


@dataclass
class OrderManager:
    live: bool = False
    use_demo: bool = False
    min_update_interval_sec: float = 3.0
    min_price_change_cents: int = 1
    state: dict[str, TickerOmsState] = field(default_factory=dict)

    def _state_for(self, ticker: str) -> TickerOmsState:
        return self.state.setdefault(ticker, TickerOmsState())

    def _should_update_side(
        self, st: TickerOmsState, resting: RestingOrder | None, target_price: int | None, target_size: int
    ) -> bool:
        now = time.time()
        if resting is None and target_size > 0:
            return True  # nothing resting, we want one -> always act (subject to interval throttle below)
        if resting is not None and target_size == 0:
            return True  # need to cancel -> always act, this reduces risk, never throttle a risk-reducing action
        if resting is None and target_size == 0:
            return False  # nothing resting, want nothing -> no-op
        # Both exist: only bother updating if price moved enough AND enough time has passed.
        if target_price is None:
            return False
        price_moved = abs(target_price - resting.price_cents) >= self.min_price_change_cents
        size_changed = target_size != resting.count
        if not (price_moved or size_changed):
            return False
        return (now - st.last_update_ts) >= self.min_update_interval_sec

    def sync(self, ticker: str, quote: strategy.Quote) -> None:
        """
        Reconciles one side at a time against the target quote. Call this
        whenever the strategy recomputes a quote — throttling inside decides
        whether anything actually happens.
        """
        st = self._state_for(ticker)
        self._sync_side(ticker, st, "bid", quote.bid_price, quote.bid_size)
        self._sync_side(ticker, st, "ask", quote.ask_price, quote.ask_size)

    def _sync_side(
        self, ticker: str, st: TickerOmsState, side: str, target_price: int | None, target_size: int
    ) -> None:
        resting = st.bid if side == "bid" else st.ask
        if not self._should_update_side(st, resting, target_price, target_size):
            return

        if resting is not None and target_size == 0:
            self._cancel(ticker, st, side, resting)
        elif resting is None and target_size > 0:
            self._create(ticker, st, side, target_price, target_size)
        elif resting is not None and target_size > 0:
            self._amend(ticker, st, side, resting, target_price, target_size)

        st.last_update_ts = time.time()

    def _create(self, ticker: str, st: TickerOmsState, side: str, price_cents: int, count: int) -> None:
        api_side = "bid" if side == "bid" else "ask"
        if not self.live:
            print(f"[oms:dry-run] would CREATE {ticker} {api_side} {count}@{price_cents}c")
            setattr(st, side, RestingOrder(order_id="dry-run", price_cents=price_cents, count=count))
            return
        resp = kalshi_orders.create_order(
            ticker=ticker, side=api_side, price_cents=price_cents, count=count, use_demo=self.use_demo
        )
        print(f"[oms:LIVE] CREATED {ticker} {api_side} {count}@{price_cents}c -> order_id={resp.get('order_id')}")
        setattr(st, side, RestingOrder(order_id=resp["order_id"], price_cents=price_cents, count=count))

    def _amend(
        self, ticker: str, st: TickerOmsState, side: str, resting: RestingOrder, price_cents: int, count: int
    ) -> None:
        api_side = "bid" if side == "bid" else "ask"
        if not self.live:
            print(f"[oms:dry-run] would AMEND {ticker} {api_side} order {resting.order_id} -> {count}@{price_cents}c")
            setattr(st, side, RestingOrder(order_id=resting.order_id, price_cents=price_cents, count=count))
            return
        kalshi_orders.amend_order(
            resting.order_id, ticker=ticker, side=api_side, price_cents=price_cents, count=count, use_demo=self.use_demo
        )
        print(f"[oms:LIVE] AMENDED {ticker} {api_side} order {resting.order_id} -> {count}@{price_cents}c")
        setattr(st, side, RestingOrder(order_id=resting.order_id, price_cents=price_cents, count=count))

    def _cancel(self, ticker: str, st: TickerOmsState, side: str, resting: RestingOrder) -> None:
        api_side = "bid" if side == "bid" else "ask"
        if not self.live:
            print(f"[oms:dry-run] would CANCEL {ticker} {api_side} order {resting.order_id}")
            setattr(st, side, None)
            return
        kalshi_orders.cancel_order(resting.order_id, use_demo=self.use_demo)
        print(f"[oms:LIVE] CANCELED {ticker} {api_side} order {resting.order_id}")
        setattr(st, side, None)

    def reset_ticker(self, ticker: str) -> None:
        """
        Forcibly cancels and drops tracking for one ticker after a sync error.
        Must cancel BOTH sides, not just the one that failed — if only the
        state dict entry is dropped, whichever side didn't error keeps
        resting on the exchange, untracked, while the next successful sync
        creates a fresh replacement alongside it. That's exactly how
        duplicate resting orders piled up on KXHIGHAUS/KXHIGHDEN: one side's
        amend 404'd, state for the ticker got dropped, and the other side's
        still-live order was orphaned instead of canceled.
        """
        st = self.state.pop(ticker, None)
        if st is None:
            return
        if st.bid is not None:
            try:
                kalshi_orders.cancel_order(st.bid.order_id, use_demo=self.use_demo)
                print(f"[oms] reset_ticker: canceled orphaned {ticker} bid {st.bid.order_id}")
            except Exception as e:
                print(f"[oms] reset_ticker: failed to cancel {ticker} bid {st.bid.order_id}: {e!s}")
        if st.ask is not None:
            try:
                kalshi_orders.cancel_order(st.ask.order_id, use_demo=self.use_demo)
                print(f"[oms] reset_ticker: canceled orphaned {ticker} ask {st.ask.order_id}")
            except Exception as e:
                print(f"[oms] reset_ticker: failed to cancel {ticker} ask {st.ask.order_id}: {e!s}")

    def cancel_all(self) -> None:
        """
        Kill switch: cancel every resting order we're tracking, live or not.
        Must never let one bad cancel (e.g. an order that already filled or
        was already removed exchange-side) stop the rest from being
        canceled — this is the last line of defense when the bot is shutting
        down, so it has to be resilient to per-order failures.
        """
        for ticker, st in self.state.items():
            if st.bid is not None:
                try:
                    self._cancel(ticker, st, "bid", st.bid)
                except Exception as e:
                    print(f"[oms] cancel_all: failed to cancel {ticker} bid {st.bid.order_id}: {e!s}")
            if st.ask is not None:
                try:
                    self._cancel(ticker, st, "ask", st.ask)
                except Exception as e:
                    print(f"[oms] cancel_all: failed to cancel {ticker} ask {st.ask.order_id}: {e!s}")
