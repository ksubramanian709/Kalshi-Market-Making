"""
Finds liquid markets to quote, via Kalshi's unauthenticated REST market-data
endpoints. Two kinds of series are supported:

- Weather bucket series (WEATHER_SERIES): one market per city per day, whose
  exact threshold shifts daily with the forecast (rediscovered fresh).
- Game series (GAME_SERIES): individual near-term game outcome markets
  (MLB/NFL/WNBA/UCL etc.) — deliberately NOT season-long championship/award
  futures, which behave more like informed-flow-dominated markets (thin
  day-to-day activity, violent jumps on trade/injury news) than the boring,
  high-volume, retail-heavy markets this bot is built for.

discover_diverse_markets() combines both: weather cities as a fixed anchor
(where the fair-value signal in weather_signal.py applies), topped up with
capped-per-league game markets so no single sport can crowd out the rest.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date

REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"

WEATHER_SERIES = [
    "KXHIGHNY", "KXHIGHLAX", "KXHIGHMIA", "KXHIGHPHIL", "KXHIGHAUS",
    "KXHIGHCHI", "KXHIGHDEN",
]
GAME_SERIES = [
    "KXMLBGAME", "KXNFLGAME", "KXNBAGAME", "KXWNBAGAME", "KXNHLGAME",
    "KXMLSGAME", "KXUCLGAME",
]
# Kept for backwards compatibility with earlier code / tests.
SERIES = WEATHER_SERIES

MIN_BID_DOLLARS = 0.03
MAX_ASK_DOLLARS = 0.97
MIN_VOLUME_24H = 200
MAX_PER_GAME_SERIES = 5


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
    """Best liquid ticker per weather city series for target_date (default: today)."""
    target_date = target_date or date.today()
    tickers = []
    for series in WEATHER_SERIES:
        ticker = find_liquid_market(series, target_date)
        if ticker:
            tickers.append(ticker)
        else:
            print(f"[market_discovery] no liquid market found for {series} on {target_date}")
    return tickers


def _event_base(ticker: str) -> str:
    """
    Strips the trailing '-SIDE' (e.g. '-IND', '-PHI') to identify the
    underlying event a market belongs to. Two markets sharing an event base
    are opposite sides of the SAME game — e.g.
    'KXWNBAGAME-26AUG02INDMIN-IND' and 'KXWNBAGAME-26AUG02INDMIN-MIN' both
    resolve on the same Indiana-vs-Minnesota game. Holding both isn't
    diversification, it's the same directional bet doubled up: if Indiana
    wins, a long-IND position and a short-MIN position both pay off
    together; if Minnesota wins, both lose together.
    """
    return ticker.rsplit("-", 1)[0]


def find_liquid_game_markets(series: str, limit: int = MAX_PER_GAME_SERIES) -> list[tuple[float, str]]:
    """
    Up to `limit` liquid open markets in a game series, as (volume, ticker),
    sorted by volume desc, with at most one side per underlying event.
    """
    url = f"{REST_BASE}/markets?series_ticker={series}&status=open&limit=100"
    try:
        data = _fetch_json(url)
    except Exception:
        return []

    candidates = []
    for m in data.get("markets", []):
        bid = _as_float(m, "yes_bid_dollars")
        ask = _as_float(m, "yes_ask_dollars")
        volume = _as_float(m, "volume_24h_fp")
        if bid > MIN_BID_DOLLARS and ask < MAX_ASK_DOLLARS and volume > MIN_VOLUME_24H:
            candidates.append((volume, m["ticker"]))

    candidates.sort(reverse=True)

    deduped: list[tuple[float, str]] = []
    seen_events: set[str] = set()
    for volume, ticker in candidates:
        event = _event_base(ticker)
        if event in seen_events:
            continue
        seen_events.add(event)
        deduped.append((volume, ticker))
        if len(deduped) >= limit:
            break
    return deduped


def discover_diverse_markets(total_limit: int = 20, target_date: date | None = None) -> list[str]:
    """
    Weather cities (fixed anchor, one bucket each) topped up with capped-per-league
    game markets, ranked by volume, capped at total_limit overall.
    """
    target_date = target_date or date.today()
    weather_tickers = discover_active_markets(target_date)

    game_candidates: list[tuple[float, str]] = []
    for series in GAME_SERIES:
        found = find_liquid_game_markets(series)
        if not found:
            print(f"[market_discovery] no liquid markets found for {series}")
        game_candidates.extend(found)

    game_candidates.sort(reverse=True)
    remaining_slots = max(total_limit - len(weather_tickers), 0)
    game_tickers = [t for _, t in game_candidates[:remaining_slots]]

    return weather_tickers + game_tickers
