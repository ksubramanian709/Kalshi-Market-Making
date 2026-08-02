"""
SQLite persistence for order book top-of-book, full depth snapshots, and trades.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_top (
    ticker TEXT NOT NULL,
    ts REAL NOT NULL,
    best_bid INTEGER,
    best_bid_qty INTEGER,
    best_ask INTEGER,
    best_ask_qty INTEGER
);
CREATE INDEX IF NOT EXISTS idx_book_top_ticker_ts ON book_top(ticker, ts);

CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    ts REAL NOT NULL,
    yes_levels_json TEXT NOT NULL,
    no_levels_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_book_snapshots_ticker_ts ON book_snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS trades (
    ticker TEXT NOT NULL,
    ts REAL NOT NULL,
    trade_id TEXT,
    yes_price INTEGER,
    count INTEGER,
    taker_side TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades(ticker, ts);

CREATE TABLE IF NOT EXISTS quotes (
    ticker TEXT NOT NULL,
    ts REAL NOT NULL,
    bid_price INTEGER,
    bid_size INTEGER,
    ask_price INTEGER,
    ask_size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_quotes_ticker_ts ON quotes(ticker, ts);

CREATE TABLE IF NOT EXISTS fills (
    ticker TEXT NOT NULL,
    ts REAL NOT NULL,
    side TEXT NOT NULL,
    price INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    cash_after REAL NOT NULL,
    position_after INTEGER NOT NULL,
    model TEXT NOT NULL DEFAULT 'optimistic'
);
CREATE INDEX IF NOT EXISTS idx_fills_ticker_ts ON fills(ticker, ts);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    ts REAL NOT NULL,
    cash REAL NOT NULL,
    positions_json TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'optimistic'
);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_ts ON portfolio_snapshots(ts);

CREATE TABLE IF NOT EXISTS fair_values (
    ticker TEXT NOT NULL,
    ts REAL NOT NULL,
    forecast_temp REAL,
    fair_value_cents INTEGER,
    market_mid_cents REAL
);
CREATE INDEX IF NOT EXISTS idx_fair_values_ticker_ts ON fair_values(ticker, ts);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # Lightweight migration for DBs created before the `model` column existed.
    _ensure_column(conn, "fills", "model", "model TEXT NOT NULL DEFAULT 'optimistic'")
    _ensure_column(conn, "portfolio_snapshots", "model", "model TEXT NOT NULL DEFAULT 'optimistic'")
    conn.commit()
    return conn


def insert_book_top(conn: sqlite3.Connection, ticker: str, top: dict, ts: float | None = None) -> None:
    conn.execute(
        "INSERT INTO book_top (ticker, ts, best_bid, best_bid_qty, best_ask, best_ask_qty) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            ticker,
            ts if ts is not None else time.time(),
            top.get("best_bid"),
            top.get("best_bid_qty"),
            top.get("best_ask"),
            top.get("best_ask_qty"),
        ),
    )
    conn.commit()


def insert_book_snapshot(conn: sqlite3.Connection, ticker: str, depth: dict, ts: float | None = None) -> None:
    conn.execute(
        "INSERT INTO book_snapshots (ticker, ts, yes_levels_json, no_levels_json) VALUES (?, ?, ?, ?)",
        (
            ticker,
            ts if ts is not None else time.time(),
            json.dumps(depth.get("yes") or []),
            json.dumps(depth.get("no") or []),
        ),
    )
    conn.commit()


def insert_trade(conn: sqlite3.Connection, ticker: str, msg: dict, ts: float | None = None) -> None:
    conn.execute(
        "INSERT INTO trades (ticker, ts, trade_id, yes_price, count, taker_side) VALUES (?, ?, ?, ?, ?, ?)",
        (
            ticker,
            ts if ts is not None else time.time(),
            msg.get("trade_id"),
            msg.get("yes_price"),
            msg.get("count"),
            msg.get("taker_side"),
        ),
    )
    conn.commit()


def insert_quote(conn: sqlite3.Connection, ticker: str, quote, ts: float | None = None) -> None:
    conn.execute(
        "INSERT INTO quotes (ticker, ts, bid_price, bid_size, ask_price, ask_size) VALUES (?, ?, ?, ?, ?, ?)",
        (
            ticker,
            ts if ts is not None else time.time(),
            quote.bid_price,
            quote.bid_size,
            quote.ask_price,
            quote.ask_size,
        ),
    )
    conn.commit()


def insert_fill(
    conn: sqlite3.Connection,
    ticker: str,
    side: str,
    price: int,
    qty: int,
    cash_after: float,
    position_after: int,
    ts: float | None = None,
    model: str = "optimistic",
) -> None:
    conn.execute(
        "INSERT INTO fills (ticker, ts, side, price, qty, cash_after, position_after, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticker, ts if ts is not None else time.time(), side, price, qty, cash_after, position_after, model),
    )
    conn.commit()


def insert_portfolio_snapshot(
    conn: sqlite3.Connection,
    cash: float,
    positions: dict[str, int],
    ts: float | None = None,
    model: str = "optimistic",
) -> None:
    conn.execute(
        "INSERT INTO portfolio_snapshots (ts, cash, positions_json, model) VALUES (?, ?, ?, ?)",
        (ts if ts is not None else time.time(), cash, json.dumps(positions), model),
    )
    conn.commit()


def insert_fair_value(
    conn: sqlite3.Connection,
    ticker: str,
    forecast_temp: float | None,
    fair_value_cents: int | None,
    market_mid_cents: float | None,
    ts: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fair_values (ticker, ts, forecast_temp, fair_value_cents, market_mid_cents) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker, ts if ts is not None else time.time(), forecast_temp, fair_value_cents, market_mid_cents),
    )
    conn.commit()
