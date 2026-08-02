"""
Read-only status report: current book, current quotes, fill history, and
portfolio P&L. Safe to run while main.py is running against the same DB
(SQLite WAL mode allows concurrent readers).
"""
from __future__ import annotations

import argparse
import json
import sqlite3

import storage


def latest_per_ticker(conn: sqlite3.Connection, table: str, cols: str) -> list[tuple]:
    return conn.execute(
        f"""
        SELECT {cols} FROM {table} t
        WHERE ts = (SELECT MAX(ts) FROM {table} WHERE ticker = t.ticker)
        ORDER BY ticker
        """
    ).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi MM status report")
    parser.add_argument("--db-path", default="data/kalshi_mm.db")
    parser.add_argument(
        "--starting-cash",
        type=float,
        default=None,
        help="Starting cash you passed to main.py, to compute P&L (optional)",
    )
    args = parser.parse_args()

    conn = storage.connect(args.db_path)

    print("=== Current book (top of book) ===")
    for ticker, ts, bb, bbq, ba, baq in latest_per_ticker(
        conn, "book_top", "ticker, ts, best_bid, best_bid_qty, best_ask, best_ask_qty"
    ):
        print(f"  {ticker}: bid {bb}c x{bbq}  ask {ba}c x{baq}")

    print("\n=== Current quotes ===")
    for ticker, ts, bp, bs, ap, aszs in latest_per_ticker(
        conn, "quotes", "ticker, ts, bid_price, bid_size, ask_price, ask_size"
    ):
        print(f"  {ticker}: bid {bp}c x{bs}  ask {ap}c x{aszs}")

    print("\n=== Fills (optimistic) ===")
    fills = conn.execute(
        "SELECT ticker, datetime(ts, 'unixepoch', 'localtime'), side, price, qty, cash_after, position_after "
        "FROM fills WHERE model='optimistic' ORDER BY ts"
    ).fetchall()
    if not fills:
        print("  (none yet)")
    for ticker, ts, side, price, qty, cash_after, pos_after in fills:
        print(f"  {ts}  {ticker}  {side} {qty}@{price}c  cash={cash_after:.2f}  pos={pos_after}")

    n_conservative_fills = conn.execute(
        "SELECT COUNT(*) FROM fills WHERE model='conservative'"
    ).fetchone()[0]
    print(f"\n=== Fills (conservative / queue-aware): {n_conservative_fills} total ===")

    mids = {}
    for ticker, _ts2, bb, _bbq, ba, _baq in latest_per_ticker(
        conn, "book_top", "ticker, ts, best_bid, best_bid_qty, best_ask, best_ask_qty"
    ):
        if bb is not None and ba is not None:
            mids[ticker] = (bb + ba) / 2

    def portfolio_summary(model: str) -> tuple[float, dict, float] | None:
        row = conn.execute(
            "SELECT cash, positions_json FROM portfolio_snapshots WHERE model=? ORDER BY ts DESC LIMIT 1",
            (model,),
        ).fetchone()
        if not row:
            return None
        cash, positions_json = row
        positions = json.loads(positions_json)
        mtm = cash + sum(pos * (mids[t] / 100) for t, pos in positions.items() if t in mids)
        return cash, positions, mtm

    print("\n=== Portfolio: optimistic vs conservative (queue-aware) ===")
    opt = portfolio_summary("optimistic")
    cons = portfolio_summary("conservative")
    if opt:
        cash, positions, mtm = opt
        print(f"  [optimistic]   cash: {cash:.2f}  mark-to-market: {mtm:.2f}  positions: {positions}")
        if args.starting_cash is not None:
            print(f"                 P&L vs starting cash ({args.starting_cash:.2f}): {mtm - args.starting_cash:+.2f}")
    else:
        print("  [optimistic]   (no snapshot yet)")
    if cons:
        cash_c, positions_c, mtm_c = cons
        print(f"  [conservative] cash: {cash_c:.2f}  mark-to-market: {mtm_c:.2f}  positions: {positions_c}")
        if args.starting_cash is not None:
            print(f"                 P&L vs starting cash ({args.starting_cash:.2f}): {mtm_c - args.starting_cash:+.2f}")
    else:
        print("  [conservative] (no snapshot yet)")
    if opt and cons:
        gap = opt[2] - cons[2]
        print(f"\n  >>> Fill-model gap (optimistic mtm - conservative mtm): {gap:+.2f} <<<")
        print("      This is the quantified effect of ignoring queue priority — the answer to")
        print("      'how much would this affect us before going live.'")

    conn.close()


if __name__ == "__main__":
    main()
