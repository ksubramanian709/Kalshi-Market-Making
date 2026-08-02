"""
Spread quoting with inventory skew and an optional fair-value signal: center
the quote on our own fair-value estimate (when available) instead of the
market midpoint, shift it toward flattening as position grows, then post a
bid/ask N cents around that center.

The fair-value center is clamped to within max_divergence_cents of the
market midpoint — if our signal is wrong or the data source breaks, we still
can't quote wildly away from where the market actually is.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Quote:
    bid_price: int | None
    bid_size: int
    ask_price: int | None
    ask_size: int


def compute_quotes(
    top: dict,
    *,
    half_spread_cents: int,
    quote_size: int,
    position: int,
    max_position: int,
    max_skew_cents: int = 0,
    fair_value_cents: int | None = None,
    max_divergence_cents: int = 15,
) -> Quote:
    """
    top: OrderBook.top_of_book() dict (best_bid/best_ask in cents, or None if empty).
    position: current net YES contracts held for this market.
    max_position: hard cap; stop quoting the side that would grow |position| past it.
    max_skew_cents: at |position| == max_position, shift the whole quote by this
        many cents toward flattening (0 disables skew, recovering the plain
        static-spread behavior).
    fair_value_cents: our own probability estimate (0-100), or None to fall back
        to plain market-midpoint quoting.
    max_divergence_cents: clamp on how far fair_value_cents can pull the quote
        center away from the market midpoint, regardless of how confident (or
        wrong) the signal is.
    """
    best_bid = top.get("best_bid")
    best_ask = top.get("best_ask")
    if best_bid is None or best_ask is None:
        return Quote(None, 0, None, 0)

    mid = (best_bid + best_ask) / 2
    if fair_value_cents is not None:
        base_center = max(mid - max_divergence_cents, min(mid + max_divergence_cents, fair_value_cents))
    else:
        base_center = mid

    # Short (position < 0) -> positive skew -> shift quote up (more eager to buy back).
    # Long (position > 0) -> negative skew -> shift quote down (more eager to sell off).
    skew = -(position / max_position) * max_skew_cents if max_position else 0
    center = base_center + skew

    bid_price = max(1, min(99, round(center - half_spread_cents)))
    ask_price = max(1, min(99, round(center + half_spread_cents)))
    if bid_price >= ask_price:
        ask_price = min(99, bid_price + 1)

    # Clip to remaining room under the cap, not just on/off — otherwise a single
    # trade can blow through max_position by up to quote_size once we're close to it.
    bid_size = max(0, min(quote_size, max_position - position))
    ask_size = max(0, min(quote_size, max_position + position))
    return Quote(bid_price, bid_size, ask_price, ask_size)
