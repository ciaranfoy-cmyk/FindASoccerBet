#!/usr/bin/env python3
"""Exchange price log -- every fixture the picks run scores, with the
model's number and every exchange price we can see, so we can later
check how Kalshi / Polymarket / Novig prices compare with the model and
with what actually happened.

Why: the Bet365 closing-price feed (statsapi) ended 2026-10-31. On the
out-of-fold history the Bet365 price alone predicted Over 2.5 better than
the model (Brier 0.2381 vs 0.2425, AUC 0.61 vs 0.58), and the xG+odds
model tier that uses it validated clearly better than xG alone. To bring
a market price back into the model we first need a history of exchange
prices, and to know whether they behave like Bet365's did. This log is
that history.

explain_picks.py calls append() on every run (one row per fixture per
run, so price moves across the week are kept too). Results are filled in
later with `settle`, and `report` compares model vs exchange vs outcome.

Where it lives: PRICE_LOG_PATH if set, else the project's shared folder
(/mnt/project-files/price_log/price_log.csv) when it exists, so the log
outlives each cloud session; otherwise data/price_log.csv.

Usage:
    python3 price_log.py settle    # fill in final scores for games played
    python3 price_log.py report    # model vs exchange vs results so far
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

import numpy as np
import pandas as pd

SHARED_DIR = "/mnt/project-files"
COLUMNS = [
    "logged_at", "fixture_id", "competition", "kickoff", "home_team", "away_team",
    "model_used", "model_p", "effective_bar", "clears_bar", "flags",
    "kalshi_yes_ask", "kalshi_no_ask", "kalshi_fair_p",
    "poly_yes_ask", "poly_no_ask", "poly_fair_p",
    "novig_yes_ask", "novig_no_ask", "novig_fair_p",
    "settled", "home_goals", "away_goals", "over_2_5",
]


def log_path() -> str:
    if os.environ.get("PRICE_LOG_PATH"):
        return os.environ["PRICE_LOG_PATH"]
    if os.path.isdir(SHARED_DIR):
        return os.path.join(SHARED_DIR, "price_log", "price_log.csv")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "price_log.csv")


def _read(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path)


def _write(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def append(live_df: pd.DataFrame) -> int:
    """Add one row per fixture from a picks run. Never raises: logging must
    not break a picks run."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        rows = pd.DataFrame({
            "logged_at": now,
            "fixture_id": live_df["fixture_id"],
            "competition": live_df["competition"],
            "kickoff": live_df.get("kickoff", live_df["date"]),
            "home_team": live_df["home_team"],
            "away_team": live_df["away_team"],
            "model_used": live_df.get("model_used"),
            "model_p": pd.to_numeric(live_df["calibrated_p"], errors="coerce").round(4),
            "effective_bar": pd.to_numeric(live_df.get("effective_bar"), errors="coerce").round(4),
            "clears_bar": live_df.get("clears_bar"),
            "flags": [len(f) if isinstance(f, list) else 0 for f in live_df.get("flags", [[]] * len(live_df))],
        })
        for col in COLUMNS:
            if col in live_df.columns and col not in rows.columns:
                rows[col] = pd.to_numeric(live_df[col], errors="coerce")
        rows["settled"] = False
        rows = rows.reindex(columns=COLUMNS)
        path = log_path()
        log = _read(path)
        out = rows if log.empty else pd.concat([log, rows], ignore_index=True)
        _write(out, path)
        print(f"Logged {len(rows)} fixtures' prices to {path}")
        return len(rows)
    except Exception as exc:  # noqa: BLE001 -- logging is best-effort
        print(f"(price log skipped: {exc})")
        return 0


def settle() -> int:
    import apifootball

    path = log_path()
    log = _read(path)
    if log.empty:
        print("Price log is empty.")
        return 0
    now = pd.Timestamp.now(tz="UTC")
    todo = log[(log["settled"] != True) & (pd.to_datetime(log["kickoff"], utc=True) < now - pd.Timedelta(hours=3))]  # noqa: E712
    ids = sorted(set(int(i) for i in todo["fixture_id"]))
    results = {}
    for i in range(0, len(ids), 20):  # API-Football takes up to 20 ids per call
        chunk = "-".join(str(x) for x in ids[i:i + 20])
        data = apifootball.get("/fixtures", {"ids": chunk}, ttl_seconds=600)
        for m in data.get("response", []):
            if m["fixture"]["status"]["short"] in ("FT", "AET", "PEN"):
                results[m["fixture"]["id"]] = (m["goals"]["home"], m["goals"]["away"])
    for col in ("settled", "home_goals", "away_goals", "over_2_5"):
        log[col] = log[col].astype(object)
    n = 0
    for idx in todo.index:
        res = results.get(int(log.at[idx, "fixture_id"]))
        if res is None:
            continue
        log.at[idx, "home_goals"], log.at[idx, "away_goals"] = res
        log.at[idx, "over_2_5"] = (res[0] + res[1]) >= 3
        log.at[idx, "settled"] = True
        n += 1
    _write(log, path)
    print(f"Settled {n} rows ({len(results)} fixtures) in {path}")
    return 0


def report() -> int:
    log = _read(log_path())
    s = log[log["settled"] == True].copy()  # noqa: E712
    if s.empty:
        print("Nothing settled yet.")
        return 0
    # Last snapshot taken before kickoff, one row per fixture.
    s = s[pd.to_datetime(s["logged_at"], utc=True) < pd.to_datetime(s["kickoff"], utc=True)]
    s = s.sort_values("logged_at").groupby("fixture_id").tail(1)
    s["over_2_5"] = s["over_2_5"].astype(str).str.lower().eq("true")
    s["exchange_fair"] = s[["kalshi_fair_p", "poly_fair_p", "novig_fair_p"]].mean(axis=1)
    y = s["over_2_5"].astype(float)
    print(f"Settled fixtures: {len(s)}  Over rate {y.mean()*100:.1f}%")
    both = s.dropna(subset=["model_p", "exchange_fair"])
    if len(both):
        yb = both["over_2_5"].astype(float)
        print(f"With an exchange price: {len(both)}")
        print(f"  Brier  model {((both['model_p']-yb)**2).mean():.4f}   exchange {((both['exchange_fair']-yb)**2).mean():.4f}")
        print(f"  mean   model {both['model_p'].mean()*100:.1f}%   exchange {both['exchange_fair'].mean()*100:.1f}%   actual {yb.mean()*100:.1f}%")
        picks = both[both["clears_bar"].astype(str).str.lower().eq("true")]
        if len(picks):
            print(f"  bar-clearing picks: {len(picks)}  hit {picks['over_2_5'].mean()*100:.1f}%  "
                  f"exchange said {picks['exchange_fair'].mean()*100:.1f}%  model said {picks['model_p'].mean()*100:.1f}%")
        both = both.copy()
        both["band"] = pd.cut(both["exchange_fair"], [0, .5, .6, .65, .7, .75, 1])
        g = both.groupby("band", observed=True).agg(n=("over_2_5", "size"), exchange=("exchange_fair", "mean"),
                                                    model=("model_p", "mean"), hit=("over_2_5", "mean"))
        print((g * [1, 100, 100, 100]).round(1).to_string())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["settle", "report", "path"])
    args = ap.parse_args()
    if args.command == "settle":
        return settle()
    if args.command == "report":
        return report()
    print(log_path())
    return 0


if __name__ == "__main__":
    sys.exit(main())
