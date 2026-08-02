#!/bin/bash
# Wrapper so launchd has one stable command to invoke. Edit the --market list
# or strategy params here, then `launchctl kickstart -k` to apply (see README/commands
# printed by setup_launchd.sh).
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
source .env.sh

exec python -u main.py \
  --market KXHIGHLAX-26AUG02-B80.5 \
  --market KXHIGHNY-26AUG02-B82.5 \
  --market KXHIGHMIA-26AUG02-B92.5 \
  --market KXHIGHNY-26AUG02-B80.5 \
  --market KXHIGHPHIL-26AUG02-B91.5 \
  --market KXHIGHAUS-26AUG02-B99.5 \
  --half-spread-cents 1 \
  --quote-size 10 \
  --max-position 50 \
  --max-skew-cents 1 \
  --max-divergence-cents 0 \
  --fair-value-refresh-sec 600 \
  --starting-cash 1000
