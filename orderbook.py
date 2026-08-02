"""
Local order book state for a single Kalshi market, built from
orderbook_snapshot + orderbook_delta WebSocket messages.

Kalshi quotes YES and NO separately, each 1-99 cents. We track both sides
as {price_cents: quantity} maps and drop levels once quantity hits 0.

Wire format (observed on the live orderbook_delta channel, not just what the
docs imply): prices and quantities are sent as decimal-dollar strings, not
integer cents —
  orderbook_snapshot.msg: {"yes_dollars_fp": [["0.0100","1346.00"], ...], "no_dollars_fp": [...]}
  orderbook_delta.msg:    {"side": "no", "price_dollars": "0.7000", "delta_fp": "-5.00"}
"""
from __future__ import annotations


def _cents(price_dollars: str) -> int:
    return round(float(price_dollars) * 100)


def _qty(qty_fp: str) -> int:
    return round(float(qty_fp))


class OrderBook:
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.yes: dict[int, int] = {}
        self.no: dict[int, int] = {}

    def apply_snapshot(self, msg: dict) -> None:
        self.yes = {_cents(p): _qty(q) for p, q in (msg.get("yes_dollars_fp") or [])}
        self.no = {_cents(p): _qty(q) for p, q in (msg.get("no_dollars_fp") or [])}

    def apply_delta(self, msg: dict) -> None:
        side = msg.get("side")
        price_dollars = msg.get("price_dollars")
        delta_fp = msg.get("delta_fp")
        if side not in ("yes", "no") or price_dollars is None or delta_fp is None:
            return
        price = _cents(price_dollars)
        delta = _qty(delta_fp)
        book = self.yes if side == "yes" else self.no
        new_qty = book.get(price, 0) + delta
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty

    def best_bid(self) -> tuple[int, int] | None:
        """Highest-priced resting YES buy order: (price, qty)."""
        if not self.yes:
            return None
        price = max(self.yes)
        return price, self.yes[price]

    def best_ask(self) -> tuple[int, int] | None:
        """Lowest-priced resting NO buy order, expressed as a YES ask: (100 - price, qty)."""
        if not self.no:
            return None
        price = max(self.no)
        return 100 - price, self.no[price]

    def top_of_book(self) -> dict:
        bid = self.best_bid()
        ask = self.best_ask()
        return {
            "best_bid": bid[0] if bid else None,
            "best_bid_qty": bid[1] if bid else None,
            "best_ask": ask[0] if ask else None,
            "best_ask_qty": ask[1] if ask else None,
        }

    def depth_snapshot(self) -> dict:
        return {
            "yes": sorted(self.yes.items(), key=lambda kv: -kv[0]),
            "no": sorted(self.no.items(), key=lambda kv: -kv[0]),
        }
