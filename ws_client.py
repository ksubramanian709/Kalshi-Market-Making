"""
Kalshi WebSocket client for market-making: subscribes to orderbook_delta +
trade channels for a set of markets, maintains local OrderBook state per
ticker, computes static-spread quotes, and simulates fills against real
trade prints. No real orders are ever placed here — see portfolio.py for
the paper P&L tracking this feeds.

https://docs.kalshi.com/getting_started/quick_start_websockets
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import ssl
from collections.abc import Callable

import certifi
import websockets
from websockets.exceptions import WebSocketException

import queue_model
import storage
import strategy
import weather_signal
from orderbook import OrderBook
from portfolio import PaperPortfolio

WS_URL_PROD = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_URL_DEMO = "wss://demo-api.kalshi.co/trade-api/ws/v2"


def websocket_url() -> str:
    return os.environ.get("KALSHI_WS_URL") or (
        WS_URL_DEMO if os.environ.get("KALSHI_USE_DEMO") else WS_URL_PROD
    )


def _as_header_list(headers: dict[str, str]) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers.items()]


def _ssl_context() -> ssl.SSLContext:
    """Use certifi CA bundle (fixes macOS python.org builds missing system certs)."""
    return ssl.create_default_context(cafile=certifi.where())


def _fresh_headers(get_headers: Callable[[], dict[str, str] | None]) -> dict[str, str]:
    h = get_headers()
    if not h:
        raise RuntimeError("WebSocket auth headers unavailable")
    return h


def _parse_trade(msg: dict) -> dict:
    """
    Normalize a trade message to {yes_price (cents), count, taker_side, trade_id}.
    Kalshi's live payloads use dollar-string fields (yes_price_dollars, count_fp)
    for orderbook/ticker channels; trade messages are assumed to match but we
    fall back to the plain integer fields the docs describe in case they don't.
    """
    if "yes_price_dollars" in msg:
        yes_price = round(float(msg["yes_price_dollars"]) * 100)
    else:
        yes_price = msg.get("yes_price")

    if "count_fp" in msg:
        count = round(float(msg["count_fp"]))
    else:
        count = msg.get("count")

    return {
        "trade_id": msg.get("trade_id"),
        "yes_price": yes_price,
        "count": count,
        "taker_side": msg.get("taker_side"),
    }


def _assert_not_crossed(ticker: str, book: OrderBook) -> None:
    """
    A real order book can never have best_bid > best_ask — any real crossing
    gets matched instantly on the exchange. If our local book ever shows one,
    our tracked state has diverged from reality (missed message, a bug in
    delta application, anything) and every quote computed from it is suspect.
    Checking the seq number catches missed messages specifically; this checks
    the actual invariant, catching that AND any other cause.
    """
    top = book.top_of_book()
    bb, ba = top.get("best_bid"), top.get("best_ask")
    if bb is not None and ba is not None and bb > ba:
        raise RuntimeError(f"{ticker}: local book crossed (bid={bb} > ask={ba}) — forcing resync")


def _requote(
    conn: sqlite3.Connection,
    ticker: str,
    book: OrderBook,
    portfolio: PaperPortfolio,
    quotes: dict[str, strategy.Quote],
    fair_values: dict[str, int | None],
    queue_states: dict[str, queue_model.QueueState],
    *,
    half_spread_cents: int,
    quote_size: int,
    max_position: int,
    max_skew_cents: int,
    max_divergence_cents: int,
) -> None:
    """Recompute our quote for a ticker and log it if it changed."""
    prev = quotes.get(ticker)
    quote = strategy.compute_quotes(
        book.top_of_book(),
        half_spread_cents=half_spread_cents,
        quote_size=quote_size,
        position=portfolio.position(ticker),
        max_position=max_position,
        max_skew_cents=max_skew_cents,
        fair_value_cents=fair_values.get(ticker),
        max_divergence_cents=max_divergence_cents,
    )
    quotes[ticker] = quote
    if prev is None or (prev.bid_price, prev.bid_size, prev.ask_price, prev.ask_size) != (
        quote.bid_price,
        quote.bid_size,
        quote.ask_price,
        quote.ask_size,
    ):
        storage.insert_quote(conn, ticker, quote)

    # Sync the queue-aware (conservative) fill model's notion of "how much real
    # size is resting ahead of us" — only actually resets when the price
    # changed (see queue_model.sync_quote_price). Uses the real book's depth
    # at our (possibly new) quote price, which by construction never includes
    # our own hypothetical order.
    qs = queue_states.setdefault(ticker, queue_model.QueueState())
    resting_at_bid = book.yes.get(quote.bid_price, 0) if quote.bid_price is not None else 0
    resting_at_ask = book.no.get(100 - quote.ask_price, 0) if quote.ask_price is not None else 0
    queue_model.sync_quote_price(
        qs,
        new_bid_price=quote.bid_price,
        new_ask_price=quote.ask_price,
        resting_at_bid=resting_at_bid,
        resting_at_ask=resting_at_ask,
    )


def _simulate_fill(
    conn: sqlite3.Connection,
    ticker: str,
    trade: dict,
    quote: strategy.Quote,
    portfolio: PaperPortfolio,
) -> bool:
    """
    Simplified fill model: if a real trade prints at or through our resting
    price, assume we'd have been filled. This ignores queue priority ahead of
    us, so it's an optimistic upper bound on real fill frequency, not a
    faithful match-engine simulation.

    Returns True if a fill was simulated (caller should requote afterward).
    """
    price = trade.get("yes_price")
    count = trade.get("count")
    if price is None or count is None:
        return False

    if quote.bid_size > 0 and quote.bid_price is not None and price <= quote.bid_price:
        qty = min(count, quote.bid_size)
        if qty <= 0:
            return False
        portfolio.apply_fill(ticker, "buy_yes", quote.bid_price, qty)
        storage.insert_fill(
            conn, ticker, "buy_yes", quote.bid_price, qty, portfolio.cash, portfolio.position(ticker)
        )
        print(f"[fill] {ticker} BUY {qty}@{quote.bid_price}c cash={portfolio.cash:.2f} pos={portfolio.position(ticker)}")
        return True

    if quote.ask_size > 0 and quote.ask_price is not None and price >= quote.ask_price:
        qty = min(count, quote.ask_size)
        if qty <= 0:
            return False
        portfolio.apply_fill(ticker, "sell_yes", quote.ask_price, qty)
        storage.insert_fill(
            conn, ticker, "sell_yes", quote.ask_price, qty, portfolio.cash, portfolio.position(ticker)
        )
        print(f"[fill] {ticker} SELL {qty}@{quote.ask_price}c cash={portfolio.cash:.2f} pos={portfolio.position(ticker)}")
        return True

    return False


def _simulate_conservative_fill(
    conn: sqlite3.Connection,
    ticker: str,
    trade: dict,
    quote: strategy.Quote,
    qs: queue_model.QueueState,
    portfolio_conservative: PaperPortfolio,
) -> None:
    """
    Queue-aware counterpart to _simulate_fill: only fills once real trade
    volume at this price has consumed the real depth that was resting ahead
    of us. Evaluated against the SAME quote as the optimistic model (see
    queue_model.py) so any P&L difference between the two isolates the
    fill-assumption effect specifically, not a difference in what we quoted.
    """
    price = trade.get("yes_price")
    count = trade.get("count")
    if price is None or count is None:
        return

    bid_fill, ask_fill = queue_model.on_trade(
        qs,
        price,
        count,
        quote_bid_price=quote.bid_price,
        quote_bid_size=quote.bid_size,
        quote_ask_price=quote.ask_price,
        quote_ask_size=quote.ask_size,
    )

    if bid_fill > 0:
        portfolio_conservative.apply_fill(ticker, "buy_yes", quote.bid_price, bid_fill)
        storage.insert_fill(
            conn, ticker, "buy_yes", quote.bid_price, bid_fill,
            portfolio_conservative.cash, portfolio_conservative.position(ticker), model="conservative",
        )
        print(
            f"[fill:conservative] {ticker} BUY {bid_fill}@{quote.bid_price}c "
            f"cash={portfolio_conservative.cash:.2f} pos={portfolio_conservative.position(ticker)}"
        )

    if ask_fill > 0:
        portfolio_conservative.apply_fill(ticker, "sell_yes", quote.ask_price, ask_fill)
        storage.insert_fill(
            conn, ticker, "sell_yes", quote.ask_price, ask_fill,
            portfolio_conservative.cash, portfolio_conservative.position(ticker), model="conservative",
        )
        print(
            f"[fill:conservative] {ticker} SELL {ask_fill}@{quote.ask_price}c "
            f"cash={portfolio_conservative.cash:.2f} pos={portfolio_conservative.position(ticker)}"
        )


async def _refresh_fair_values(
    conn: sqlite3.Connection,
    books: dict[str, OrderBook],
    fair_values: dict[str, int | None],
    interval_sec: float,
) -> None:
    """
    Periodically re-fetch the weather fair-value signal per ticker in a
    worker thread (NWS calls are blocking) so it never stalls the WS
    receive loop. Fetches immediately on startup, then on interval_sec.
    """
    while True:
        for ticker in books:
            try:
                result = await asyncio.to_thread(weather_signal.evaluate, ticker)
            except Exception as e:
                print(f"[fair_value] {ticker} fetch failed: {e!s}")
                result = None
            fair_values[ticker] = result["fair_value_cents"] if result else None
            top = books[ticker].top_of_book()
            mid = (
                (top["best_bid"] + top["best_ask"]) / 2
                if top.get("best_bid") is not None and top.get("best_ask") is not None
                else None
            )
            storage.insert_fair_value(
                conn,
                ticker,
                result["forecast_temp"] if result else None,
                result["fair_value_cents"] if result else None,
                mid,
            )
            if result:
                print(
                    f"[fair_value] {ticker} forecast={result['forecast_temp']}F "
                    f"std={result['std_dev']} fair_value={result['fair_value_cents']}c market_mid={mid}"
                )
        await asyncio.sleep(interval_sec)


async def _periodic_snapshots(
    conn: sqlite3.Connection,
    books: dict[str, OrderBook],
    portfolio: PaperPortfolio,
    portfolio_conservative: PaperPortfolio,
    interval_sec: float,
) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        mids = {}
        for ticker, book in books.items():
            storage.insert_book_snapshot(conn, ticker, book.depth_snapshot())
            top = book.top_of_book()
            if top.get("best_bid") is not None and top.get("best_ask") is not None:
                mids[ticker] = (top["best_bid"] + top["best_ask"]) / 2

        mtm = portfolio.mark_to_market(mids)
        storage.insert_portfolio_snapshot(conn, portfolio.cash, dict(portfolio.positions), model="optimistic")
        print(f"[portfolio] cash={portfolio.cash:.2f} positions={portfolio.positions} mark_to_market={mtm:.2f}")

        mtm_c = portfolio_conservative.mark_to_market(mids)
        storage.insert_portfolio_snapshot(
            conn, portfolio_conservative.cash, dict(portfolio_conservative.positions), model="conservative"
        )
        print(
            f"[portfolio:conservative] cash={portfolio_conservative.cash:.2f} "
            f"positions={portfolio_conservative.positions} mark_to_market={mtm_c:.2f}"
        )


async def stream_orderbook(
    conn: sqlite3.Connection,
    tickers: list[str],
    get_headers: Callable[[], dict[str, str] | None],
    portfolio: PaperPortfolio,
    portfolio_conservative: PaperPortfolio,
    *,
    url: str | None = None,
    snapshot_interval_sec: float = 5.0,
    half_spread_cents: int = 2,
    quote_size: int = 10,
    max_position: int = 50,
    max_skew_cents: int = 0,
    max_divergence_cents: int = 15,
    fair_value_refresh_sec: float = 600.0,
) -> None:
    """
    Connect, subscribe to orderbook_delta + trade for the given tickers,
    maintain local book state, quote around our fair-value estimate (when
    available) with inventory skew, and simulate fills against real trade
    prints into `portfolio` (optimistic: any crossing trade fills us) and,
    in parallel against the identical quotes, into `portfolio_conservative`
    (queue-aware: only fills once real volume clears the depth resting ahead
    of us) — see queue_model.py. Comparing the two quantifies how much the
    optimistic model's fill-frequency assumption actually matters.
    """
    ws_url = url or websocket_url()
    ticker_set = set(tickers)
    books: dict[str, OrderBook] = {t: OrderBook(t) for t in tickers}
    quotes: dict[str, strategy.Quote] = {}
    fair_values: dict[str, int | None] = {}
    queue_states: dict[str, queue_model.QueueState] = {t: queue_model.QueueState() for t in tickers}
    backoff_s = 1.0
    msg_id = 1

    snapshot_task = asyncio.create_task(
        _periodic_snapshots(conn, books, portfolio, portfolio_conservative, snapshot_interval_sec)
    )
    fair_value_task = asyncio.create_task(
        _refresh_fair_values(conn, books, fair_values, fair_value_refresh_sec)
    )
    try:
        while True:
            try:
                headers = _as_header_list(_fresh_headers(get_headers))
                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ssl=_ssl_context(),
                    ping_interval=20,
                    ping_timeout=25,
                    close_timeout=5,
                ) as ws:
                    backoff_s = 1.0
                    last_seq_by_sid: dict[int, int] = {}
                    sub = {
                        "id": msg_id,
                        "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": tickers},
                    }
                    msg_id += 1
                    await ws.send(json.dumps(sub))

                    while True:
                        raw = await ws.recv()
                        data = json.loads(raw)
                        kind = data.get("type")

                        # Kalshi tags each message on a channel subscription (sid) with an
                        # incrementing seq. A gap means we missed a message — most
                        # dangerously an orderbook_delta, which would silently leave our
                        # local book permanently wrong (stale phantom price levels) since
                        # we otherwise only resync via a fresh snapshot on reconnect. Force
                        # that reconnect immediately rather than keep quoting off a
                        # possibly-corrupted book.
                        sid = data.get("sid")
                        seq = data.get("seq")
                        if sid is not None and seq is not None:
                            expected = last_seq_by_sid.get(sid)
                            if expected is not None and seq != expected + 1:
                                raise RuntimeError(
                                    f"orderbook seq gap on sid={sid}: expected {expected + 1}, got {seq}"
                                )
                            last_seq_by_sid[sid] = seq

                        msg = data.get("msg") or {}
                        ticker = msg.get("market_ticker")

                        if kind == "orderbook_snapshot" and ticker in ticker_set:
                            book = books[ticker]
                            book.apply_snapshot(msg)
                            _assert_not_crossed(ticker, book)
                            storage.insert_book_snapshot(conn, ticker, book.depth_snapshot())
                            storage.insert_book_top(conn, ticker, book.top_of_book())
                            _requote(
                                conn, ticker, book, portfolio, quotes, fair_values, queue_states,
                                half_spread_cents=half_spread_cents,
                                quote_size=quote_size, max_position=max_position,
                                max_skew_cents=max_skew_cents,
                                max_divergence_cents=max_divergence_cents,
                            )
                        elif kind == "orderbook_delta" and ticker in ticker_set:
                            book = books[ticker]
                            book.apply_delta(msg)
                            _assert_not_crossed(ticker, book)
                            storage.insert_book_top(conn, ticker, book.top_of_book())
                            _requote(
                                conn, ticker, book, portfolio, quotes, fair_values, queue_states,
                                half_spread_cents=half_spread_cents,
                                quote_size=quote_size, max_position=max_position,
                                max_skew_cents=max_skew_cents,
                                max_divergence_cents=max_divergence_cents,
                            )
                        elif kind == "trade" and ticker in ticker_set:
                            trade = _parse_trade(msg)
                            storage.insert_trade(conn, ticker, trade)
                            print(
                                f"[trade] {ticker} yes_price={trade['yes_price']} "
                                f"count={trade['count']} taker={trade['taker_side']}"
                            )
                            quote = quotes.get(ticker)
                            if quote is not None:
                                qs = queue_states.setdefault(ticker, queue_model.QueueState())
                                _simulate_conservative_fill(conn, ticker, trade, quote, qs, portfolio_conservative)
                                if _simulate_fill(conn, ticker, trade, quote, portfolio):
                                    _requote(
                                        conn, ticker, books[ticker], portfolio, quotes, fair_values, queue_states,
                                        half_spread_cents=half_spread_cents,
                                        quote_size=quote_size, max_position=max_position,
                                        max_skew_cents=max_skew_cents,
                                        max_divergence_cents=max_divergence_cents,
                                    )
                        elif kind == "error":
                            err = msg or {}
                            print(f"[WS error] {err.get('code')}: {err.get('msg')}")
                        elif kind == "subscribed":
                            print(f"[WS] subscribed: {data.get('msg')}")
                        elif kind == "ok":
                            pass

            except (WebSocketException, OSError, RuntimeError, json.JSONDecodeError) as e:
                print(f"[WS] {e!s}; reconnecting in {backoff_s:.0f}s")
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 60.0)
    finally:
        snapshot_task.cancel()
        fair_value_task.cancel()
