#!/usr/bin/env python3
"""Ledger of REAL Kalshi fills, so P&L on actual trades can be tracked
over time instead of trusting memory. Two steps:

  record  -- pull real fills from the account (kalshi_trading.get_fills)
    and append any not already in data/kalshi_trades.csv, keyed by
    fill_id so nothing is double-logged. This is the source of truth
    for what was actually filled, not what a script tried to place.

  settle  -- for logged rows still marked unsettled, check the real
    market result (public GET /markets/{ticker}) and, once it has
    resolved, fill in the payout and realized pnl for that fill at the
    price it actually filled at.

data/kalshi_trades.csv is append-only for new fills and update-in-place
only for the settled/result/payout_dollars/pnl_dollars/settled_at
columns of existing rows -- the trade terms themselves are never
rewritten.

Usage:
    python3 kalshi_trade_log.py record
    python3 kalshi_trade_log.py settle
    python3 kalshi_trade_log.py summary
"""

import argparse
import csv
import json
import os
import urllib.request
from datetime import datetime, timezone

from kalshi_trading import KalshiError, get_fills

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kalshi_trades.csv")
KALSHI_PUBLIC_BASE = "https://api.elections.kalshi.com/trade-api/v2"

FIELDS = [
    "fill_id", "order_id", "ticker", "action", "side", "count", "price_dollars",
    "fee_dollars", "cost_dollars", "created_time", "logged_at",
    "settled", "result", "payout_dollars", "pnl_dollars", "settled_at",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_rows() -> list[dict]:
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _get_market(ticker: str) -> dict:
    with urllib.request.urlopen(f"{KALSHI_PUBLIC_BASE}/markets/{ticker}", timeout=20) as resp:
        return json.load(resp)["market"]


def record() -> int:
    """Append any real fill not already logged. Returns count of new rows."""
    rows = _read_rows()
    known_ids = {r["fill_id"] for r in rows}

    try:
        fills = get_fills().get("fills", [])
    except KalshiError as exc:
        print(f"Error fetching fills: {exc}")
        return 0

    new_rows = []
    for fill in fills:
        if fill["fill_id"] in known_ids:
            continue
        side = fill["side"]
        price = float(fill["yes_price_dollars"] if side == "yes" else fill["no_price_dollars"])
        count = float(fill["count_fp"])
        fee = float(fill["fee_cost"])
        cost = count * price + fee
        new_rows.append({
            "fill_id": fill["fill_id"],
            "order_id": fill["order_id"],
            "ticker": fill["ticker"],
            "action": fill["action"],
            "side": side,
            "count": count,
            "price_dollars": price,
            "fee_dollars": fee,
            "cost_dollars": round(cost, 4),
            "created_time": fill["created_time"],
            "logged_at": _now(),
            "settled": "False",
            "result": "",
            "payout_dollars": "",
            "pnl_dollars": "",
            "settled_at": "",
        })

    if new_rows:
        rows.extend(new_rows)
        _write_rows(rows)
    print(f"Logged {len(new_rows)} new fill(s); {len(rows)} total in {LOG_PATH}")
    return len(new_rows)


def settle() -> int:
    """Check real market results for logged rows still unsettled.
    Returns count of rows newly settled.
    """
    rows = _read_rows()
    unsettled = [r for r in rows if r["settled"] != "True"]
    if not unsettled:
        print("Nothing unsettled to check.")
        return 0

    checked_tickers: dict[str, dict] = {}
    n_settled = 0
    for row in unsettled:
        ticker = row["ticker"]
        if ticker not in checked_tickers:
            try:
                checked_tickers[ticker] = _get_market(ticker)
            except Exception as exc:  # noqa: BLE001 -- best-effort, keep going on network hiccups
                print(f"Could not fetch {ticker}: {exc}")
                continue
        market = checked_tickers[ticker]
        result = market.get("result", "")
        if not result:
            continue  # market hasn't settled yet

        count = float(row["count"])
        cost = float(row["cost_dollars"])
        won = (row["action"] == "buy" and row["side"] == result) or (
            row["action"] == "sell" and row["side"] != result
        )
        payout = count * 1.00 if won else 0.0
        pnl = payout - cost if row["action"] == "buy" else cost - payout

        row["settled"] = "True"
        row["result"] = result
        row["payout_dollars"] = round(payout, 4)
        row["pnl_dollars"] = round(pnl, 4)
        row["settled_at"] = _now()
        n_settled += 1

    if n_settled:
        _write_rows(rows)
    print(f"Settled {n_settled} row(s); {len(unsettled) - n_settled} still open.")
    return n_settled


def summary() -> None:
    rows = _read_rows()
    if not rows:
        print("No trades logged yet.")
        return

    settled_rows = [r for r in rows if r["settled"] == "True"]
    open_rows = [r for r in rows if r["settled"] != "True"]

    total_cost = sum(float(r["cost_dollars"]) for r in rows)
    realized_pnl = sum(float(r["pnl_dollars"]) for r in settled_rows)
    open_exposure = sum(float(r["cost_dollars"]) for r in open_rows)
    wins = sum(1 for r in settled_rows if float(r["pnl_dollars"]) > 0)

    print(f"Total fills logged:   {len(rows)}")
    print(f"Total real $ risked:  ${total_cost:.2f}")
    print(f"Settled trades:       {len(settled_rows)} ({wins} won, {len(settled_rows) - wins} lost)")
    print(f"Realized P&L:         ${realized_pnl:+.2f}")
    print(f"Open trades:          {len(open_rows)} (${open_exposure:.2f} still at risk)")
    if settled_rows:
        print(f"Avg P&L per settled trade: ${realized_pnl / len(settled_rows):+.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("record", help="Pull real fills and append new ones to the ledger")
    sub.add_parser("settle", help="Check real results for open ledger rows and compute pnl")
    sub.add_parser("summary", help="Print running P&L across the ledger")

    args = parser.parse_args()
    if args.cmd == "record":
        record()
    elif args.cmd == "settle":
        settle()
    elif args.cmd == "summary":
        summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
