"""
Simulated (paper) portfolio: tracks cash and per-market YES position from
simulated fills. No real orders are ever placed — see ws_client.py for the
trade-crossing fill simulation that calls into this.
"""
from __future__ import annotations


class PaperPortfolio:
    def __init__(self, starting_cash: float):
        self.cash = starting_cash
        self.positions: dict[str, int] = {}

    def position(self, ticker: str) -> int:
        return self.positions.get(ticker, 0)

    def apply_fill(self, ticker: str, side: str, price_cents: int, qty: int) -> None:
        """side: 'buy_yes' (we bought YES, i.e. our bid got hit) or 'sell_yes' (our ask got lifted)."""
        notional = (price_cents / 100) * qty
        if side == "buy_yes":
            self.cash -= notional
            self.positions[ticker] = self.position(ticker) + qty
        elif side == "sell_yes":
            self.cash += notional
            self.positions[ticker] = self.position(ticker) - qty
        else:
            raise ValueError(f"unknown fill side: {side}")

    def mark_to_market(self, mid_prices_cents: dict[str, int]) -> float:
        """Cash + sum(position * mid price) for tickers with a known mid price."""
        value = self.cash
        for ticker, pos in self.positions.items():
            mid = mid_prices_cents.get(ticker)
            if mid is not None:
                value += pos * (mid / 100)
        return value
