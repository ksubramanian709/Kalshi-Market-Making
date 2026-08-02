#!/bin/bash
# The one command to check results: ~/kalshi-mm/status.sh
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
python report.py --starting-cash 5000
