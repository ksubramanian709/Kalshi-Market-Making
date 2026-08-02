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

    print("\n=== Fills (queue-aware — the realistic number) ===")
    fills = conn.execute(
        "SELECT ticker, datetime(ts, 'unixepoch', 'localtime'), side, price, qty, cash_after, position_after "
        "FROM fills WHERE model='conservative' ORDER BY ts"
    ).fetchall()
    if not fills:
        print("  (none yet)")
    for ticker, ts, side, price, qty, cash_after, pos_after in fills:
        print(f"  {ts}  {ticker}  {side} {qty}@{price}c  cash={cash_after:.2f}  pos={pos_after}")

    n_optimistic_fills = conn.execute(
        "SELECT COUNT(*) FROM fills WHERE model='optimistic'"
    ).fetchone()[0]
    print(f"\n  (reference only: the optimistic/no-queue-priority model logged {n_optimistic_fills} fills over the same period)")

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

    print("\n=== Portfolio (queue-aware — treat this as the real number) ===")
    cons = portfolio_summary("conservative")
    opt = portfolio_summary("optimistic")
    if cons:
        cash_c, positions_c, mtm_c = cons
        print(f"  cash: {cash_c:.2f}")
        print(f"  positions: {positions_c}")
        print(f"  mark-to-market: {mtm_c:.2f}")
        if args.starting_cash is not None:
            print(f"  P&L vs starting cash ({args.starting_cash:.2f}): {mtm_c - args.starting_cash:+.2f}")
    else:
        print("  (no snapshot yet)")

    print("\n  --- reference only, not the number to make decisions on ---")
    if opt:
        cash, positions, mtm = opt
        print(f"  [optimistic, ignores queue priority] mark-to-market: {mtm:.2f}", end="")
        if args.starting_cash is not None:
            print(f"  (P&L {mtm - args.starting_cash:+.2f})")
        else:
            print()
    else:
        print("  [optimistic] (no snapshot yet)")
    if opt and cons:
        gap = opt[2] - cons[2]
        print(f"  gap (optimistic - queue-aware): {gap:+.2f} — how much the naive model overstates P&L right now")

    conn.close()


if __name__ == "__main__":
    main()
