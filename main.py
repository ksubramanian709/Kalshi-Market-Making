"""
Market-making paper-trading entry point: connects to Kalshi, subscribes to
order book + trade data, quotes a static spread around the midpoint, and
simulates fills against real trade prints into a paper (simulated) cash +
position ledger. No real orders are ever placed.

Reading Kalshi's real/prod market data here carries no capital risk on its
own — the risk boundary is "do we place real orders," which this tool does
not do. Set KALSHI_USE_DEMO=1 instead if you want to point at Kalshi's demo
sandbox, though note its order books are largely empty (no simulated
counterparties), so real market data is more useful for exercising this
strategy/fill logic even during paper trading.

Env:
  KALSHI_API_KEY_ID       — key id from Kalshi (demo or prod, matching KALSHI_USE_DEMO)
  KALSHI_PRIVATE_KEY_PATH — path to PEM private key
  KALSHI_USE_DEMO=1       — use Kalshi's demo sandbox instead of real market data
  KALSHI_WS_URL           — override the WebSocket URL entirely
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import kalshi_auth
import storage
import ws_client
from portfolio import PaperPortfolio


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kalshi market-making paper trader")
    parser.add_argument(
        "--market",
        action="append",
        required=True,
        help="Market ticker to subscribe to. Repeat --market for multiple markets.",
    )
    parser.add_argument(
        "--db-path",
        default="data/kalshi_mm.db",
        help="SQLite database path (default: data/kalshi_mm.db)",
    )
    parser.add_argument(
        "--snapshot-interval-sec",
        type=float,
        default=60.0,
        help="How often to persist a full order-book depth snapshot + portfolio snapshot (default: 60s)",
    )
    parser.add_argument(
        "--half-spread-cents",
        type=int,
        default=2,
        help="Quote at midpoint +/- this many cents (default: 2)",
    )
    parser.add_argument(
        "--quote-size",
        type=int,
        default=10,
        help="Contracts to quote on each side (default: 10)",
    )
    parser.add_argument(
        "--max-position",
        type=int,
        default=50,
        help="Stop quoting a side once |position| would exceed this, per market (default: 50)",
    )
    parser.add_argument(
        "--max-skew-cents",
        type=int,
        default=None,
        help="At |position| == max_position, shift the whole quote this many cents toward "
        "flattening (more eager to reduce inventory). 0 disables skew. Defaults to "
        "--half-spread-cents, so a fully-loaded position quotes right at the market on the "
        "side that would flatten it.",
    )
    parser.add_argument(
        "--starting-cash",
        type=float,
        default=5000.0,
        help="Simulated starting cash for the paper portfolio (default: 1000.0)",
    )
    parser.add_argument(
        "--max-divergence-cents",
        type=int,
        default=15,
        help="Cap on how far our weather fair-value signal can pull the quote center away "
        "from the market midpoint (default: 15). 0 disables the fair-value signal entirely, "
        "recovering plain market-midpoint quoting.",
    )
    parser.add_argument(
        "--fair-value-refresh-sec",
        type=float,
        default=600.0,
        help="How often to re-fetch the NWS forecast per market (default: 600s / 10min)",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    max_skew_cents = args.max_skew_cents if args.max_skew_cents is not None else args.half_spread_cents
    conn = storage.connect(args.db_path)
    portfolio = PaperPortfolio(starting_cash=args.starting_cash)
    print(
        f"[main] logging to {args.db_path}, markets={args.market}, "
        f"starting_cash={args.starting_cash}, half_spread={args.half_spread_cents}c, "
        f"quote_size={args.quote_size}, max_position={args.max_position}, "
        f"max_skew_cents={max_skew_cents}, max_divergence_cents={args.max_divergence_cents}, "
        f"fair_value_refresh_sec={args.fair_value_refresh_sec}"
    )
    try:
        await ws_client.stream_orderbook(
            conn,
            args.market,
            kalshi_auth.build_websocket_headers,
            portfolio,
            snapshot_interval_sec=args.snapshot_interval_sec,
            half_spread_cents=args.half_spread_cents,
            quote_size=args.quote_size,
            max_position=args.max_position,
            max_skew_cents=max_skew_cents,
            max_divergence_cents=args.max_divergence_cents,
            fair_value_refresh_sec=args.fair_value_refresh_sec,
        )
    finally:
        conn.close()


def main() -> None:
    reason = kalshi_auth.websocket_auth_failure_reason()
    if reason:
        print(f"[main] auth config error: {reason}", file=sys.stderr)
        sys.exit(1)
    if not kalshi_auth.build_websocket_headers():
        print(
            "[main] missing KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH.",
            file=sys.stderr,
        )
        sys.exit(1)

    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[main] shutting down")


if __name__ == "__main__":
    main()
