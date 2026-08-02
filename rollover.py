"""
Refreshes data/active_markets.txt with a diverse set of liquid markets —
weather cities as a fixed anchor, topped up with capped-per-league near-term
game markets (MLB/NFL/WNBA/UCL/etc.), up to --limit total. Run once daily
(before today's markets have had time to expire) — see
com.kalshimm.rollover.plist, which forces a bot restart right after this
runs so main.py picks up the fresh ticker list.

Never overwrites an existing, working ticker list with an empty one — if
discovery comes back empty (e.g. run too early before the new day's markets
have built up liquidity), the previous list is left in place.
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import market_discovery

DEFAULT_PATH = "data/active_markets.txt"


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the active market ticker list")
    parser.add_argument("--out-path", default=DEFAULT_PATH)
    parser.add_argument("--limit", type=int, default=20, help="Max total markets (default: 20)")
    args = parser.parse_args()

    out_path = Path(args.out_path)
    tickers = market_discovery.discover_diverse_markets(total_limit=args.limit, target_date=date.today())

    if not tickers:
        if out_path.exists():
            print(f"[rollover] discovery returned nothing; leaving existing {out_path} untouched")
        else:
            print("[rollover] discovery returned nothing and no existing list — bot will have no markets")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(tickers) + "\n")
    print(f"[rollover] wrote {len(tickers)} tickers to {out_path}: {tickers}")


if __name__ == "__main__":
    main()
