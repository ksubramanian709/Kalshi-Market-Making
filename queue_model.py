"""
Conservative ("queue-aware") fill simulation, run in parallel with the
existing optimistic model (ws_client.py's _simulate_fill) to quantify how
much queue priority actually matters — a concrete answer to "how much would
ignoring queue priority affect us" without needing live capital.

Model: when we start quoting at a price, we assume we're placed at the BACK
of whatever real resting size already exists there (book depth at that
price, from the real order book — never includes our own hypothetical
order). As real trades print at that price, they consume that resting size
first; only once cumulative traded volume exceeds what was ahead of us do
WE start getting filled, up to our own quoted size. This is still a
simplification (real matching can be more nuanted, e.g. partial cancels
ahead of us), but it's a meaningfully more realistic lower bound than
"any crossing trade fills us," which the existing model uses as an upper
bound. Real fill behavior should sit somewhere between the two.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QueueState:
    bid_price: int | None = None
    bid_ahead: int = 0
    bid_consumed: int = 0
    bid_filled: int = 0
    ask_price: int | None = None
    ask_ahead: int = 0
    ask_consumed: int = 0
    ask_filled: int = 0


def sync_quote_price(
    qs: QueueState,
    *,
    new_bid_price: int | None,
    new_ask_price: int | None,
    resting_at_bid: int,
    resting_at_ask: int,
) -> None:
    """
    Call whenever the quote is (re)computed. Resets queue tracking for a side
    only when that side's price actually changed (a new price means a new
    queue — the old resting depth ahead of us at the old price no longer
    applies).
    """
    if new_bid_price != qs.bid_price:
        qs.bid_price = new_bid_price
        qs.bid_ahead = resting_at_bid
        qs.bid_consumed = 0
        qs.bid_filled = 0
    if new_ask_price != qs.ask_price:
        qs.ask_price = new_ask_price
        qs.ask_ahead = resting_at_ask
        qs.ask_consumed = 0
        qs.ask_filled = 0


def on_trade(
    qs: QueueState,
    trade_price: int,
    trade_qty: int,
    *,
    quote_bid_price: int | None,
    quote_bid_size: int,
    quote_ask_price: int | None,
    quote_ask_size: int,
) -> tuple[int, int]:
    """Returns (bid_fill_qty, ask_fill_qty) this trade produces under the queue model."""
    bid_fill = 0
    ask_fill = 0

    if quote_bid_size > 0 and quote_bid_price is not None and trade_price <= quote_bid_price:
        qs.bid_consumed += trade_qty
        available = qs.bid_consumed - qs.bid_ahead - qs.bid_filled
        if available > 0:
            bid_fill = min(available, quote_bid_size - qs.bid_filled)
            qs.bid_filled += bid_fill

    if quote_ask_size > 0 and quote_ask_price is not None and trade_price >= quote_ask_price:
        qs.ask_consumed += trade_qty
        available = qs.ask_consumed - qs.ask_ahead - qs.ask_filled
        if available > 0:
            ask_fill = min(available, quote_ask_size - qs.ask_filled)
            qs.ask_filled += ask_fill

    return bid_fill, ask_fill
