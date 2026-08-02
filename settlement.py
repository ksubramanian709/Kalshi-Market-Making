"""
Looks up a market's actual settlement result so closed positions can be
priced at their true $0/$1 outcome instead of being silently excluded from
mark-to-market once the market stops quoting a live bid/ask — an exclusion
that understates losses and overstates gains on anything that's settled
against us.
"""
from __future__ import annotations

import json
import urllib.request

REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Once a market settles, its result never changes — safe to cache forever
# for the lifetime of the process.
_cache: dict[str, int | None] = {}


def get_settlement_cents(ticker: str) -> int | None:
    """100 if resolved YES, 0 if resolved NO, None if not yet settled or lookup failed."""
    if ticker in _cache:
        return _cache[ticker]

    result = None
    try:
        req = urllib.request.Request(
            f"{REST_BASE}/markets/{ticker}", headers={"User-Agent": "kalshi-mm"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            market = json.loads(resp.read())["market"]
        outcome = market.get("result")
        if outcome == "yes":
            result = 100
        elif outcome == "no":
            result = 0
    except Exception:
        pass  # leave as None; caller falls back to excluding the position

    if result is not None:
        _cache[ticker] = result
    return result
