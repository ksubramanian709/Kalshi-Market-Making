#!/bin/bash
# Wrapper so launchd has one stable command to invoke. Refreshes the active
# market list (rollover.py) on every launch, so both a crash-triggered
# restart and the daily com.kalshimm.rollover job naturally pick up fresh
# tickers as each day's markets expire. Edit strategy params below, then
# `launchctl kickstart -k gui/$(id -u)/com.kalshimm.bot` to apply.
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
source .env.sh

python rollover.py

MARKET_ARGS=()
while IFS= read -r ticker; do
  [ -n "$ticker" ] && MARKET_ARGS+=(--market "$ticker")
done < data/active_markets.txt

if [ ${#MARKET_ARGS[@]} -eq 0 ]; then
  echo "[run_bot] no active markets found in data/active_markets.txt, aborting" >&2
  exit 1
fi

exec python -u main.py \
  "${MARKET_ARGS[@]}" \
  --half-spread-cents 5 \
  --quote-size 50 \
  --max-position 250 \
  --max-skew-cents 5 \
  --max-divergence-cents 0 \
  --fair-value-refresh-sec 600 \
  --snapshot-interval-sec 60 \
  --starting-cash 5000
