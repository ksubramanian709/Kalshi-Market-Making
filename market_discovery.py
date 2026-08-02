"""
Finds the most liquid daily-high-temperature bucket market per city series
for a given date, via Kalshi's unauthenticated REST market-data endpoints.
Used by rollover.py to pick fresh tickers as each day's markets expire.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date

REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Same 5 cities used throughout the project; each city's actual bucket
# threshold (e.g. B82.5 vs B84.5) shifts daily with the forecast, so we
# rediscover the liquid one fresh each day rather than hardcoding it.
SERIES = ["KXHIGHNY", "KXHIGHLAX", "KXHIGHMIA", "KXHIGHPHIL", "KXHIGHAUS"]

MIN_BID_DOLLARS = 0.03
MAX_ASK_DOLLARS = 0.97
MIN_VOLUME_24H = 200


def _ticker_date_suffix(target_date: date) -> str:
    # Kalshi ticker date format: YYMONDD, e.g. 26AUG02
    return target_date.strftime("%y%b%d").upper()


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-mm-rollover"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _as_float(m: dict, key: str) -> float:
    try:
        return float(m.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def find_liquid_market(series: str, target_date: date) -> str | None:
    """Most liquid open 'B' bucket market in `series` for `target_date`, or None."""
    date_suffix = _ticker_date_suffix(target_date)
    url = f"{REST_BASE}/markets?series_ticker={series}&status=open&limit=50"
    try:
        data = _fetch_json(url)
    except Exception:
        return None

    candidates = []
    for m in data.get("markets", []):
        ticker = m.get("ticker", "")
        if f"-{date_suffix}-B" not in ticker:
            continue
        bid = _as_float(m, "yes_bid_dollars")
        ask = _as_float(m, "yes_ask_dollars")
        volume = _as_float(m, "volume_24h_fp")
        if bid > MIN_BID_DOLLARS and ask < MAX_ASK_DOLLARS and volume > MIN_VOLUME_24H:
            candidates.append((volume, ticker))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def discover_active_markets(target_date: date | None = None) -> list[str]:
    """Best liquid ticker per city series for target_date (default: today, UTC)."""
    target_date = target_date or date.today()
    tickers = []
    for series in SERIES:
        ticker = find_liquid_market(series, target_date)
        if ticker:
            tickers.append(ticker)
        else:
            print(f"[market_discovery] no liquid market found for {series} on {target_date}")
    return tickers
