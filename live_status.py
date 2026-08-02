"""
Generates a standalone HTML status snapshot of the real (live) Kalshi
account: balance, positions, resting orders, and risk-limit utilization.
Pulls directly from Kalshi's REST API (kalshi_orders.py) — never from local
state — so this is always the exchange's own ground truth, not a guess
about what the bot thinks it did.

Usage: python live_status.py [--out data/live_status.html]
"""
from __future__ import annotations

import argparse
import html
import subprocess
from datetime import datetime, timezone

import kalshi_orders as ko

STARTING_CASH = 294.0
RISK_MAX_POSITION_PER_MARKET = 3
RISK_MAX_TOTAL_NOTIONAL = 100.0
RISK_MAX_LOSS = 30.0

def find_live_trader() -> tuple[str, str, list[str]] | None:
    """
    Returns (pid, elapsed, tracked_markets) for the running live_trader.py
    process, or None. Markets are parsed straight out of its actual command
    line — never a hardcoded list or a rollover file — so this always
    reflects what's really being quoted, even after a mid-session restart
    with a different --market set.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,etime,command"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "live_trader.py" in line and "--live" in line:
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, elapsed, cmd = parts[0], parts[1], parts[2]
            args = cmd.split()
            markets = [args[i + 1] for i in range(len(args) - 1) if args[i] == "--market"]
            return pid, elapsed, markets
    return None


def fmt_money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def fmt_cents(price_dollars: str | None) -> str:
    if price_dollars is None:
        return "&mdash;"
    return f"{round(float(price_dollars) * 100)}&cent;"


def gather() -> dict:
    balance = ko.get_balance()
    positions = ko.get_positions().get("market_positions", [])
    orders = ko.get_orders(status="resting").get("orders", [])
    live_info = find_live_trader()

    pos_by_ticker = {p["ticker"]: p for p in positions}
    orders_by_ticker: dict[str, dict[str, dict]] = {}
    for o in orders:
        orders_by_ticker.setdefault(o["ticker"], {})[o["book_side"]] = o

    balance_dollars = float(balance.get("balance_dollars", 0) or 0)
    pnl = balance_dollars - STARTING_CASH

    total_position_count = sum(abs(float(p.get("position_fp", 0) or 0)) for p in positions)
    realized_pnl_total = sum(float(p.get("realized_pnl_dollars", 0) or 0) for p in positions)
    open_exposure_total = sum(float(p.get("market_exposure_dollars", 0) or 0) for p in positions)

    if live_info:
        tracked_markets = live_info[2]
    else:
        # Process isn't running — fall back to whatever's still actually on
        # the account (positions + resting orders) rather than a stale list,
        # so the report never claims a market is "waiting" when really
        # nothing is tracking it at all.
        tracked_markets = sorted(set(pos_by_ticker) | set(orders_by_ticker))

    rows = []
    for ticker in tracked_markets:
        pos = pos_by_ticker.get(ticker)
        pos_count = float(pos.get("position_fp", 0) or 0) if pos else 0.0
        exposure = float(pos.get("market_exposure_dollars", 0) or 0) if pos else 0.0
        realized = float(pos.get("realized_pnl_dollars", 0) or 0) if pos else 0.0
        market_orders = orders_by_ticker.get(ticker, {})
        bid = market_orders.get("bid")
        ask = market_orders.get("ask")
        rows.append({
            "ticker": ticker,
            "position": pos_count,
            "exposure": exposure,
            "realized": realized,
            "bid_price": bid.get("yes_price_dollars") if bid else None,
            "ask_price": ask.get("yes_price_dollars") if ask else None,
            "quoting": bool(bid or ask),
        })

    return {
        "generated_at": datetime.now(timezone.utc),
        "balance": balance_dollars,
        "pnl": pnl,
        "pid": live_info[0] if live_info else None,
        "uptime": live_info[1] if live_info else None,
        "total_position_count": total_position_count,
        "realized_pnl_total": realized_pnl_total,
        "open_exposure_total": open_exposure_total,
        "rows": rows,
        "n_quoting": sum(1 for r in rows if r["quoting"]),
        "n_markets": len(rows),
    }


def render(d: dict) -> str:
    live = d["pid"] is not None
    status_label = "LIVE" if live else "STOPPED"
    status_class = "good" if live else "bad"

    pnl = d["pnl"]
    pnl_class = "good" if pnl >= 0 else ("bad" if pnl < -0.01 else "flat")
    pnl_sign = "+" if pnl > 0.005 else ("" if pnl > -0.005 else "")

    pos_pct = min(100, round(d["total_position_count"] / RISK_MAX_TOTAL_NOTIONAL * 100))
    loss_amt = max(0.0, -pnl)
    loss_pct = min(100, round(loss_amt / RISK_MAX_LOSS * 100))

    rows_html = []
    for r in d["rows"]:
        pos = r["position"]
        if pos > 0:
            dir_chip = '<span class="chip chip-long">LONG</span>'
        elif pos < 0:
            dir_chip = '<span class="chip chip-short">SHORT</span>'
        else:
            dir_chip = '<span class="chip chip-flat">FLAT</span>'

        if r["quoting"]:
            quote_html = (
                f'<span class="quote-bid">{fmt_cents(r["bid_price"])}</span>'
                f'<span class="quote-sep">/</span>'
                f'<span class="quote-ask">{fmt_cents(r["ask_price"])}</span>'
            )
        else:
            quote_html = '<span class="waiting">waiting for liquidity</span>'

        realized_class = "good" if r["realized"] > 0.001 else ("bad" if r["realized"] < -0.001 else "flat")

        rows_html.append(f"""
        <tr>
          <td class="ticker-cell">{html.escape(r["ticker"])}</td>
          <td class="num">{dir_chip}</td>
          <td class="num">{pos:+.0f}</td>
          <td class="num">{quote_html}</td>
          <td class="num">{fmt_money(r["exposure"])}</td>
          <td class="num {realized_class}">{fmt_money(r["realized"])}</td>
        </tr>""")

    generated_str = d["generated_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
    uptime_str = d["uptime"] or "&mdash;"
    pid_str = d["pid"] or "not running"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kalshi Live &mdash; Trading Status</title>
<style>
:root {{
  --ink: #10161b;
  --ink-raised: #182027;
  --ink-line: #26313a;
  --paper: #f3f0e6;
  --paper-raised: #ffffff;
  --paper-line: #dcd6c4;
  --accent: #c9944b;
  --accent-soft: #c9944b33;
  --good: #4f9d6e;
  --bad: #c1543c;
  --muted: #8b9199;

  --bg: var(--paper);
  --surface: var(--paper-raised);
  --line: var(--paper-line);
  --text: #1b1f22;
  --text-dim: #5b6066;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: var(--ink);
    --surface: var(--ink-raised);
    --line: var(--ink-line);
    --text: #e8e6de;
    --text-dim: #99a0a6;
  }}
}}
:root[data-theme="dark"] {{
  --bg: var(--ink);
  --surface: var(--ink-raised);
  --line: var(--ink-line);
  --text: #e8e6de;
  --text-dim: #99a0a6;
}}
:root[data-theme="light"] {{
  --bg: var(--paper);
  --surface: var(--paper-raised);
  --line: var(--paper-line);
  --text: #1b1f22;
  --text-dim: #5b6066;
}}

* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  padding: clamp(16px, 4vw, 48px);
  min-height: 100vh;
}}
.wrap {{ max-width: 980px; margin: 0 auto; display: flex; flex-direction: column; gap: 22px; }}

.masthead {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 16px;
}}
.masthead h1 {{
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", "Times New Roman", serif;
  font-size: clamp(24px, 3.4vw, 32px);
  font-weight: 600;
  margin: 0;
  letter-spacing: 0.2px;
  text-wrap: balance;
}}
.masthead .sub {{
  font-size: 13px;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}}

.status-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}}
.status-pill.good {{ background: color-mix(in srgb, var(--good) 18%, transparent); color: var(--good); }}
.status-pill.bad {{ background: color-mix(in srgb, var(--bad) 18%, transparent); color: var(--bad); }}
.status-pill::before {{
  content: "";
  width: 7px; height: 7px;
  border-radius: 50%;
  background: currentColor;
}}
.status-pill.good::before {{ box-shadow: 0 0 0 3px color-mix(in srgb, var(--good) 25%, transparent); }}

.hero {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: hidden;
}}
.stat {{
  background: var(--surface);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}}
.stat .label {{
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-dim);
}}
.stat .value {{
  font-family: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  font-size: clamp(20px, 2.6vw, 27px);
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}}
.stat .value.good {{ color: var(--good); }}
.stat .value.bad {{ color: var(--bad); }}

section {{
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 20px 22px;
}}
section h2 {{
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 15px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-dim);
  margin: 0 0 16px 0;
}}

.gauges {{ display: flex; flex-direction: column; gap: 16px; }}
.gauge-row {{ display: flex; flex-direction: column; gap: 6px; }}
.gauge-head {{
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: var(--text-dim);
}}
.gauge-head .amt {{
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}}
.gauge-track {{
  height: 8px;
  border-radius: 5px;
  background: color-mix(in srgb, var(--text-dim) 18%, transparent);
  overflow: hidden;
}}
.gauge-fill {{
  height: 100%;
  border-radius: 5px;
  background: var(--accent);
  transition: width 0.3s ease;
}}
.gauge-fill.bad {{ background: var(--bad); }}

table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
th {{
  text-align: left;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-dim);
  font-weight: 600;
  padding: 0 10px 10px 10px;
  border-bottom: 1px solid var(--line);
}}
td {{
  padding: 10px;
  border-bottom: 1px solid var(--line);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  vertical-align: middle;
}}
tr:last-child td {{ border-bottom: none; }}
.ticker-cell {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12.5px; color: var(--text); }}
td.num {{ text-align: right; }}
th:nth-child(1), th:nth-child(2), th:nth-child(3) {{ text-align: left; }}
th:nth-child(2), th:nth-child(3) {{ text-align: right; }}

.table-scroll {{ overflow-x: auto; }}

.chip {{
  display: inline-block;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.05em;
  padding: 2px 8px;
  border-radius: 5px;
}}
.chip-long {{ background: color-mix(in srgb, var(--good) 18%, transparent); color: var(--good); }}
.chip-short {{ background: color-mix(in srgb, var(--bad) 18%, transparent); color: var(--bad); }}
.chip-flat {{ background: color-mix(in srgb, var(--text-dim) 20%, transparent); color: var(--text-dim); }}

.quote-bid {{ color: var(--good); }}
.quote-ask {{ color: var(--bad); }}
.quote-sep {{ color: var(--text-dim); padding: 0 3px; }}
.waiting {{ color: var(--text-dim); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; font-style: italic; font-size: 12px; }}

.good {{ color: var(--good); }}
.bad {{ color: var(--bad); }}
.flat {{ color: var(--text-dim); }}

footer {{
  font-size: 12px;
  color: var(--text-dim);
  text-align: center;
  padding-top: 8px;
}}
</style>
</head>
<body>
<div class="wrap">

  <div class="masthead">
    <div>
      <h1>Live Trading Status</h1>
      <div class="sub">Kalshi &middot; real capital &middot; generated {generated_str}</div>
    </div>
    <span class="status-pill {status_class}">{status_label}</span>
  </div>

  <div class="hero">
    <div class="stat">
      <span class="label">Balance</span>
      <span class="value">{fmt_money(d["balance"])}</span>
    </div>
    <div class="stat">
      <span class="label">P&amp;L vs ${STARTING_CASH:.0f} deposit</span>
      <span class="value {pnl_class}">{pnl_sign}{fmt_money(abs(pnl)) if pnl < 0 else fmt_money(pnl)}</span>
    </div>
    <div class="stat">
      <span class="label">Markets quoting</span>
      <span class="value">{d["n_quoting"]} / {d["n_markets"]}</span>
    </div>
    <div class="stat">
      <span class="label">Process</span>
      <span class="value" style="font-size: 15px;">PID {pid_str}{" &middot; " + uptime_str if d["uptime"] else ""}</span>
    </div>
  </div>

  <section>
    <h2>Risk limit utilization</h2>
    <div class="gauges">
      <div class="gauge-row">
        <div class="gauge-head">
          <span>Total position (all markets)</span>
          <span class="amt">{d["total_position_count"]:.0f} / {RISK_MAX_TOTAL_NOTIONAL:.0f} contracts</span>
        </div>
        <div class="gauge-track"><div class="gauge-fill{' bad' if pos_pct > 80 else ''}" style="width:{pos_pct}%"></div></div>
      </div>
      <div class="gauge-row">
        <div class="gauge-head">
          <span>Drawdown from deposit</span>
          <span class="amt">{fmt_money(loss_amt)} / {fmt_money(RISK_MAX_LOSS)}</span>
        </div>
        <div class="gauge-track"><div class="gauge-fill{' bad' if loss_pct > 80 else ''}" style="width:{loss_pct}%"></div></div>
      </div>
      <div class="gauge-row">
        <div class="gauge-head">
          <span>Per-market position cap</span>
          <span class="amt">{RISK_MAX_POSITION_PER_MARKET:.0f} contracts max, per market</span>
        </div>
        <div class="gauge-track"><div class="gauge-fill" style="width:0%"></div></div>
      </div>
    </div>
  </section>

  <section>
    <h2>Markets ({d["n_markets"]})</h2>
    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Side</th>
          <th>Position</th>
          <th>Resting bid / ask</th>
          <th>Exposure</th>
          <th>Realized P&amp;L</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows_html)}
      </tbody>
    </table>
    </div>
  </section>

  <footer>
    Pulled live from Kalshi&rsquo;s own account API &mdash; not from local bot state.
  </footer>

</div>
</body>
</html>"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="data/live_status.html")
    args = p.parse_args()
    d = gather()
    html_out = render(d)
    with open(args.out, "w") as f:
        f.write(html_out)
    print(f"[live_status] wrote {args.out}")
    print(f"[live_status] balance={d['balance']:.4f} pnl={d['pnl']:.4f} pid={d['pid']} quoting={d['n_quoting']}/{d['n_markets']}")


if __name__ == "__main__":
    main()
