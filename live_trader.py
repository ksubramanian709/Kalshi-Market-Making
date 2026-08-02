"""
Live (real-order-capable) market-making entry point. Separate from main.py
(the paper simulator) so nothing about the existing, working paper bot is
disturbed by this.

Defaults to --live=False (dry-run): every decision the OMS would make is
computed and logged, but kalshi_orders is never actually called — nothing
reaches Kalshi. Pass --live to actually place real orders.

Position, for both strategy skew and the risk layer, comes from Kalshi's
own /portfolio/positions endpoint (polled), not from our own fill-tracking
— the exchange's own record is the ground truth, and given how many bugs
we found in this project's own simulated fill-tracking today, that's a
deliberate choice, not an oversight.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys

import certifi
import websockets
from websockets.exceptions import WebSocketException

import kalshi_auth
import kalshi_orders
import risk
import strategy
from oms import OrderManager
from orderbook import OrderBook


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kalshi live market maker (real orders, dry-run by default)")
    p.add_argument("--market", action="append", required=True, help="Repeat for multiple markets.")
    p.add_argument("--live", action="store_true", help="Actually place real orders. Default: dry-run only.")
    p.add_argument("--use-demo", action="store_true", help="Trade against Kalshi's demo sandbox instead of prod.")
    p.add_argument("--half-spread-cents", type=int, default=2)
    p.add_argument("--quote-size", type=int, default=1)
    p.add_argument("--max-position", type=int, default=5, help="Strategy-level position cap per market.")
    p.add_argument("--max-skew-cents", type=int, default=None)
    p.add_argument("--min-update-interval-sec", type=float, default=3.0)
    p.add_argument("--min-price-change-cents", type=int, default=1)
    p.add_argument("--position-poll-sec", type=float, default=5.0)
    p.add_argument("--starting-cash", type=float, default=300.0, help="Reference point for the loss risk check.")
    p.add_argument("--max-position-per-market", type=int, default=5, help="Risk-layer hard cap (independent of strategy).")
    p.add_argument("--max-total-notional-dollars", type=float, default=100.0)
    p.add_argument("--max-loss-dollars", type=float, default=50.0)
    return p.parse_args()


def websocket_url(use_demo: bool) -> str:
    return "wss://demo-api.kalshi.co/trade-api/ws/v2" if use_demo else "wss://api.elections.kalshi.com/trade-api/ws/v2"


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


async def _poll_positions(positions: dict[str, int], use_demo: bool, interval_sec: float) -> None:
    """Refreshes `positions` from Kalshi's own records — the ground truth, not our own fill guesses."""
    while True:
        try:
            resp = await asyncio.to_thread(kalshi_orders.get_positions, use_demo=use_demo)
            fresh = {}
            for mp in resp.get("market_positions", []):
                ticker = mp.get("ticker")
                # Kalshi reports position as a fixed-point count string; net YES position.
                qty = mp.get("position")
                if ticker is not None and qty is not None:
                    fresh[ticker] = int(round(float(qty)))
            positions.clear()
            positions.update(fresh)
        except Exception as e:
            print(f"[live] position poll failed: {e!s}")
        await asyncio.sleep(interval_sec)


async def run(args: argparse.Namespace) -> None:
    max_skew_cents = args.max_skew_cents if args.max_skew_cents is not None else args.half_spread_cents
    mode = "LIVE — REAL ORDERS WILL BE PLACED" if args.live else "DRY RUN — nothing will be sent to Kalshi"
    print(f"[live] mode: {mode}")
    print(f"[live] markets={args.market}, half_spread={args.half_spread_cents}c, quote_size={args.quote_size}, "
          f"max_position={args.max_position}, risk: per_market<={args.max_position_per_market} "
          f"notional<=${args.max_total_notional_dollars} loss<=${args.max_loss_dollars}")

    if args.live:
        confirm = input("Type EXACTLY 'yes deploy real capital' to proceed live, anything else aborts: ")
        if confirm.strip() != "yes deploy real capital":
            print("[live] aborted — confirmation phrase did not match.")
            return

    om = OrderManager(
        live=args.live,
        use_demo=args.use_demo,
        min_update_interval_sec=args.min_update_interval_sec,
        min_price_change_cents=args.min_price_change_cents,
    )
    limits = risk.RiskLimits(
        max_position_per_market=args.max_position_per_market,
        max_total_notional_dollars=args.max_total_notional_dollars,
        max_loss_dollars=args.max_loss_dollars,
    )
    risk_state = risk.RiskState()

    tickers = args.market
    ticker_set = set(tickers)
    books: dict[str, OrderBook] = {t: OrderBook(t) for t in tickers}
    real_positions: dict[str, int] = {}

    position_task = asyncio.create_task(_poll_positions(real_positions, args.use_demo, args.position_poll_sec))

    backoff_s = 1.0
    try:
        while True:
            try:
                headers = kalshi_auth.build_websocket_headers()
                if headers is None:
                    raise RuntimeError("Kalshi auth headers unavailable")
                async with websockets.connect(
                    websocket_url(args.use_demo),
                    additional_headers=list(headers.items()),
                    ssl=_ssl_context(),
                    ping_interval=20, ping_timeout=25, close_timeout=5,
                ) as ws:
                    backoff_s = 1.0
                    last_seq_by_sid: dict[int, int] = {}
                    await ws.send(json.dumps({
                        "id": 1, "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": tickers},
                    }))

                    while True:
                        raw = await ws.recv()
                        data = json.loads(raw)
                        kind = data.get("type")

                        sid, seq = data.get("sid"), data.get("seq")
                        if sid is not None and seq is not None:
                            expected = last_seq_by_sid.get(sid)
                            if expected is not None and seq != expected + 1:
                                raise RuntimeError(f"seq gap on sid={sid}")
                            last_seq_by_sid[sid] = seq

                        msg = data.get("msg") or {}
                        ticker = msg.get("market_ticker")
                        if ticker not in ticker_set:
                            continue

                        if kind == "orderbook_snapshot":
                            books[ticker].apply_snapshot(msg)
                        elif kind == "orderbook_delta":
                            books[ticker].apply_delta(msg)
                        else:
                            continue

                        top = books[ticker].top_of_book()
                        bb, ba = top.get("best_bid"), top.get("best_ask")
                        if bb is not None and ba is not None and bb > ba:
                            raise RuntimeError(f"{ticker}: local book crossed (bid={bb}>ask={ba}) — forcing resync")

                        mids = {t: (bk.top_of_book()["best_bid"] + bk.top_of_book()["best_ask"]) / 2
                                for t, bk in books.items()
                                if bk.top_of_book()["best_bid"] is not None and bk.top_of_book()["best_ask"] is not None}
                        mtm = args.starting_cash + sum(
                            real_positions.get(t, 0) * mids[t] / 100 for t in mids
                        )
                        halted = risk.check(
                            limits, risk_state, positions=real_positions,
                            mark_to_market=mtm, starting_cash=args.starting_cash,
                        )
                        if halted:
                            print(f"[live] RISK HALT: {risk_state.halt_reason} — canceling everything, no new orders")
                            om.cancel_all()
                            continue

                        quote = strategy.compute_quotes(
                            top,
                            half_spread_cents=args.half_spread_cents,
                            quote_size=args.quote_size,
                            position=real_positions.get(ticker, 0),
                            max_position=args.max_position,
                            max_skew_cents=max_skew_cents,
                        )
                        om.sync(ticker, quote)

            except (WebSocketException, OSError, RuntimeError, json.JSONDecodeError) as e:
                print(f"[live] {e!s}; reconnecting in {backoff_s:.0f}s")
                for t in om.state:
                    om.state[t].bid = None
                    om.state[t].ask = None
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 60.0)
    finally:
        position_task.cancel()
        om.cancel_all()


def main() -> None:
    reason = kalshi_auth.websocket_auth_failure_reason()
    if reason:
        print(f"[live] auth config error: {reason}", file=sys.stderr)
        sys.exit(1)
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[live] shutting down, canceling any resting orders")


if __name__ == "__main__":
    main()
