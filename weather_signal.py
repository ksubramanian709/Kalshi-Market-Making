"""
Fair-value signal for Kalshi daily-high-temperature bucket markets
(tickers like KXHIGHNY-26AUG02-B82.5, which resolves YES iff the settlement
station's high temp for that date is in [82, 83)).

Pulls a point forecast from the National Weather Service (api.weather.gov,
free, no API key) for the market's settlement station, models the actual
high as Normal(forecast, std_dev), and returns P(bucket) = CDF(upper) - CDF(lower).

std_dev scales with how much of the target day's temperature trajectory is
still unresolved — a same-day forecast made at 3am (the afternoon high
hasn't happened yet) should carry much more uncertainty than one made at
3pm (the high has likely already occurred). We also pull the day's observed
temps-so-far and use their max as a floor on the forecast: the eventual
high can't be lower than what's already been recorded.

Calibration honesty: std_dev is still a rough, unfit heuristic (not derived
from NWS's actual historical forecast-error statistics by lead time). Treat
any fair-value output as a hypothesis to validate against realized
settlements over time, not a trustworthy probability out of the box. See
main.py's docstring before ever pointing this at real capital.
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

USER_AGENT = "kalshi-mm-research (paper trading, contact: karthik)"

# (lat, lon) for the exact NWS settlement station named in each market's rules_primary.
STATION_COORDS: dict[str, tuple[float, float]] = {
    "KXHIGHNY": (40.7829, -73.9654),    # Central Park, NYC
    "KXHIGHLAX": (33.9425, -118.4081),  # LAX Airport
    "KXHIGHMIA": (25.7959, -80.2870),   # Miami International Airport
    "KXHIGHPHIL": (39.8719, -75.2411),  # Philadelphia International Airport
    "KXHIGHAUS": (30.1975, -97.6664),   # Austin-Bergstrom
}

STATION_TZ: dict[str, str] = {
    "KXHIGHNY": "America/New_York",
    "KXHIGHLAX": "America/Los_Angeles",
    "KXHIGHMIA": "America/New_York",
    "KXHIGHPHIL": "America/New_York",
    "KXHIGHAUS": "America/Chicago",
}

# Rough, unfit placeholders. Std-dev (°F) at the moment the day's high has
# just occurred (~near-certain), and additional std-dev per day of lead time
# beyond today, interpolated by hours-until-assumed-peak for same-day.
STD_DEV_AT_PEAK = 1.0
STD_DEV_ONE_DAY_AHEAD = 2.5
STD_DEV_PER_EXTRA_DAY = 1.0
STD_DEV_FALLBACK_DAYS = 5
ASSUMED_PEAK_LOCAL_HOUR = 15  # 3pm local, rough typical daily-high timing

TICKER_RE = re.compile(r"^(KXHIGH[A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-B([\d.]+)$")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
)}

_forecast_cache: dict[tuple[float, float], tuple[float, list[dict]]] = {}
_station_id_cache: dict[tuple[float, float], str] = {}
_CACHE_TTL_SEC = 1800.0


def parse_bucket_ticker(ticker: str) -> tuple[str, date, float, float] | None:
    """Returns (series, target_date, bucket_lower, bucket_upper) or None if not a
    recognized 'BXX.5' bucket ticker (e.g. T-type strikes aren't handled)."""
    m = TICKER_RE.match(ticker)
    if not m:
        return None
    series, yy, mon, dd, mid_str = m.groups()
    if mon not in MONTHS:
        return None
    target_date = date(2000 + int(yy), MONTHS[mon], int(dd))
    mid = float(mid_str)
    return series, target_date, mid - 0.5, mid + 0.5


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _get_forecast_periods(lat: float, lon: float) -> list[dict]:
    key = (lat, lon)
    now = time.time()
    cached = _forecast_cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SEC:
        return cached[1]

    points = _fetch_json(f"https://api.weather.gov/points/{lat},{lon}")
    forecast_url = points["properties"]["forecast"]
    forecast = _fetch_json(forecast_url)
    periods = forecast["properties"]["periods"]
    _forecast_cache[key] = (now, periods)
    return periods


def forecast_high_for_date(lat: float, lon: float, target_date: date) -> float | None:
    """Daytime-period forecast high (°F) for target_date, or None if unavailable."""
    periods = _get_forecast_periods(lat, lon)
    for p in periods:
        if not p.get("isDaytime"):
            continue
        start = datetime.fromisoformat(p["startTime"]).date()
        if start == target_date:
            return float(p["temperature"])
    return None


def _get_station_id(lat: float, lon: float) -> str | None:
    key = (lat, lon)
    if key in _station_id_cache:
        return _station_id_cache[key]
    points = _fetch_json(f"https://api.weather.gov/points/{lat},{lon}")
    stations = _fetch_json(points["properties"]["observationStations"])
    features = stations.get("features") or []
    if not features:
        return None
    station_id = features[0]["properties"]["stationIdentifier"]
    _station_id_cache[key] = station_id
    return station_id


def max_observed_temp_for_date(lat: float, lon: float, target_date: date, tz_name: str) -> float | None:
    """Max observed temp (°F) so far on target_date, in the station's local time, or None."""
    station_id = _get_station_id(lat, lon)
    if station_id is None:
        return None
    obs = _fetch_json(f"https://api.weather.gov/stations/{station_id}/observations?limit=48")
    tz = ZoneInfo(tz_name)
    max_temp = None
    for f in obs.get("features") or []:
        props = f["properties"]
        temp_c = (props.get("temperature") or {}).get("value")
        if temp_c is None:
            continue
        ts = datetime.fromisoformat(props["timestamp"]).astimezone(tz)
        if ts.date() != target_date:
            continue
        temp_f = temp_c * 9 / 5 + 32
        if max_temp is None or temp_f > max_temp:
            max_temp = temp_f
    return max_temp


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bucket_probability(forecast_temp: float, lower: float, upper: float, std_dev: float) -> float:
    z_upper = (upper - forecast_temp) / std_dev
    z_lower = (lower - forecast_temp) / std_dev
    return max(0.0, min(1.0, _norm_cdf(z_upper) - _norm_cdf(z_lower)))


def std_dev_for_target(target_date: date, tz_name: str, *, now: datetime | None = None) -> float:
    """
    Hours-until-assumed-peak-time scaled uncertainty for same-day targets;
    falls back to a per-day table for future days.
    """
    tz = ZoneInfo(tz_name)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    peak_today = datetime.combine(target_date, datetime.min.time(), tzinfo=tz).replace(hour=ASSUMED_PEAK_LOCAL_HOUR)
    hours_until_peak = (peak_today - now_local).total_seconds() / 3600

    if now_local.date() == target_date:
        if hours_until_peak <= 0:
            return STD_DEV_AT_PEAK
        # Linear interpolation: 0h before peak -> STD_DEV_AT_PEAK, 24h before -> ONE_DAY_AHEAD level.
        frac = min(hours_until_peak / 24.0, 1.0)
        return STD_DEV_AT_PEAK + frac * (STD_DEV_ONE_DAY_AHEAD - STD_DEV_AT_PEAK)

    lead_days = (target_date - now_local.date()).days
    if lead_days <= 0:
        return STD_DEV_AT_PEAK
    if lead_days == 1:
        return STD_DEV_ONE_DAY_AHEAD
    return STD_DEV_ONE_DAY_AHEAD + (lead_days - 1) * STD_DEV_PER_EXTRA_DAY


def evaluate(ticker: str, *, today: date | None = None) -> dict | None:
    """
    Best-effort fair-value evaluation for a KXHIGH*-B bucket ticker. Returns
    None if the ticker isn't a recognized bucket market, the city isn't
    mapped, the market's date has passed, or the NWS forecast call fails.
    Otherwise returns {forecast_temp, observed_max, std_dev, fair_value_cents}.
    """
    parsed = parse_bucket_ticker(ticker)
    if parsed is None:
        return None
    series, target_date, lower, upper = parsed
    coords = STATION_COORDS.get(series)
    tz_name = STATION_TZ.get(series)
    if coords is None or tz_name is None:
        return None

    utc_today = today or datetime.now(timezone.utc).date()
    if (target_date - utc_today).days < -1:
        return None  # market's date has clearly passed

    try:
        forecast_temp = forecast_high_for_date(*coords, target_date)
    except Exception:
        return None
    if forecast_temp is None:
        return None

    observed_max = None
    try:
        observed_max = max_observed_temp_for_date(*coords, target_date, tz_name)
    except Exception:
        pass  # non-fatal — fall back to forecast alone

    # The eventual high can't be below what's already been observed today.
    effective_temp = max(forecast_temp, observed_max) if observed_max is not None else forecast_temp

    std_dev = std_dev_for_target(target_date, tz_name)
    prob = bucket_probability(effective_temp, lower, upper, std_dev)
    return {
        "forecast_temp": forecast_temp,
        "observed_max": observed_max,
        "std_dev": std_dev,
        "fair_value_cents": round(prob * 100),
    }


def fair_value_cents(ticker: str, *, today: date | None = None) -> int | None:
    """Convenience wrapper around evaluate() for callers that just want the number."""
    result = evaluate(ticker, today=today)
    return result["fair_value_cents"] if result else None
