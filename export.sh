#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
source .env.sh
python export_csv.py --starting-cash 5000
python export_xlsx.py --starting-cash 5000
