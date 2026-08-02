"""
Exports current results to CSV files that open directly in Excel/Numbers.
Safe to run while main.py is running (SQLite WAL allows concurrent readers).
Re-run any time (e.g. on a schedule) to refresh the files in place.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path


def export_table(conn: sqlite3.Connection, query: str, out_path: Path) -> int:
    cur = conn.execute(query)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Kalshi MM results to CSV")
    parser.add_argument("--db-path", default="data/kalshi_mm.db")
    parser.add_argument("--out-dir", default="data/exports")
    parser.add_argument("--starting-cash", type=float, default=5000.0)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    out_dir = Path(args.out_dir)

    n_fills = export_table(
        conn,
        "SELECT ticker, datetime(ts, 'unixepoch', 'localtime') AS time, side, price, qty, "
        "cash_after, position_after FROM fills ORDER BY ts",
        out_dir / "fills.csv",
    )

    n_portfolio = export_table(
        conn,
        "SELECT datetime(ts, 'unixepoch', 'localtime') AS time, cash, positions_json "
        "FROM portfolio_snapshots ORDER BY ts",
        out_dir / "portfolio_history.csv",
    )

    # Current status: one row per ticker with latest book/quote/position.
    cur = conn.execute(
        """
        SELECT bt.ticker,
               datetime(bt.ts, 'unixepoch', 'localtime') AS book_time,
               bt.best_bid, bt.best_bid_qty, bt.best_ask, bt.best_ask_qty,
               q.bid_price AS our_bid, q.bid_size AS our_bid_size,
               q.ask_price AS our_ask, q.ask_size AS our_ask_size
        FROM book_top bt
        LEFT JOIN quotes q
          ON q.ticker = bt.ticker AND q.ts = (SELECT MAX(ts) FROM quotes WHERE ticker = bt.ticker)
        WHERE bt.ts = (SELECT MAX(ts) FROM book_top WHERE ticker = bt.ticker)
        ORDER BY bt.ticker
        """
    )
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    status_path = out_dir / "current_status.csv"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    with open(status_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)

    row = conn.execute(
        "SELECT cash, positions_json FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row:
        import json
        cash, positions_json = row
        positions = json.loads(positions_json)
        mids = {r[0]: (r[2] + r[4]) / 2 / 100 for r in rows if r[2] is not None and r[4] is not None}
        mtm = cash + sum(pos * mids.get(t, 0) for t, pos in positions.items())
        with open(out_dir / "pnl_summary.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["cash", "mark_to_market", "pnl_vs_starting_cash", "starting_cash"])
            writer.writerow([f"{cash:.2f}", f"{mtm:.2f}", f"{mtm - args.starting_cash:.2f}", args.starting_cash])

    conn.close()
    print(f"exported {n_fills} fills, {n_portfolio} portfolio snapshots, {len(rows)} market statuses to {out_dir}/")


if __name__ == "__main__":
    main()
