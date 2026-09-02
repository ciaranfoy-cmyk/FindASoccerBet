#!/usr/bin/env python3
"""The only real way to know if this model is profitable: track real
predictions against real Kalshi prices, going forward, and see what
actually happens. There is no historical Kalshi price archive to
backtest against (confirmed this session -- real prices only exist for
a rolling window of upcoming/recent fixtures), so profitability can
only be established prospectively, one real gameweek at a time. This
script is that tracker.

Two modes:

  snapshot -- score upcoming fixtures across all 6 leagues, pull REAL
    live Kalshi "Over 2.5" prices, compute the calibrated model
    probability and the resulting edge, and APPEND new rows to
    data/forward_test_log.csv (one row per fixture, never edited
    retroactively -- already-logged fixture_ids are skipped so the
    same pick can't be logged twice with a different price later).

  settle -- for previously logged rows whose kickoff has passed, check
    the real result via API-Football and fill in the actual outcome
    and profit/loss AT THE PRICE THAT WAS ACTUALLY LOGGED (not a
    re-fetch -- that would defeat the point).

Selection rule (fixed BEFORE any outcome is known, not chosen after
the fact): a fixture is "selected" if its calibrated probability
clears the rolling p95 bar computed from the model's own trailing
historical out-of-fold predictions -- same methodology validated in
backtest_season_rolling_percentile.py / calibration_full_history.py,
just applied to live fixtures instead of stopping at historical data.

Usage:
    APIFOOTBALL_KEY=xxxx python3 forward_test_log.py snapshot
    APIFOOTBALL_KEY=xxxx python3 forward_test_log.py settle
"""

import argparse
import csv
import os
import re
import sys
import warnings
from collections import deque

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_dataset_apifootball import add_derived_features
from analyze_player_form import add_player_form_derived_features
from analyze_shots_venue import (
    add_shots_venue_derived_features,
    load_with_player_form_and_shots_venue,
    load_with_xg_player_form_and_shots_venue,
)
from analyze_xg_features import XG_RAW_FEATURES, XG_DERIVED_FEATURES, add_xg_derived_features
from backtest_season_rolling_percentile import N_FOLDS_CORE, N_FOLDS_XG, build_stream
from build_dataset_apifootball import LEAGUES, fetch_all_fixtures
from calibration import apply_calibration, load_calibrators
from live_kalshi_edge_test import kalshi_get, _normalize
from predict_upcoming import (
    CORE_CANDIDATES,
    XG_CANDIDATES,
    build_feature_row,
    fetch_upcoming_fixtures,
    replay_to_current_state,
)

warnings.filterwarnings("ignore")

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "forward_test_log.csv")
FIELDS = [
    "logged_at", "fixture_id", "competition", "kickoff_date", "home_team", "away_team",
    "model_used", "raw_p", "calibrated_p", "rolling_p95_bar", "selected",
    "kalshi_ticker", "kalshi_yes_ask", "kalshi_yes_bid", "kalshi_implied_p", "edge_vs_ask",
    "settled", "actual_home_goals", "actual_away_goals", "actual_over_2_5",
    "pnl_per_dollar_if_selected",
]

KALSHI_SERIES_BY_COMPETITION = {
    "PL": "KXEPLTOTAL",
    "ELC": "KXEFLCHAMPIONSHIPTOTAL",
    "LALIGA": "KXLALIGATOTAL",
    "BUNDESLIGA": "KXBUNDESLIGATOTAL",
    "SERIEA": "KXSERIEATOTAL",
    "LIGUE1": "KXLIGUE1TOTAL",
    "MLS": "KXMLSTOTAL",
    "EREDIVISIE": "KXEREDIVISIETOTAL",
    "SUPERLIG": "KXSUPERLIGTOTAL",
}


def fetch_kalshi_over25_for_series(series_ticker: str) -> list[dict]:
    events = kalshi_get("/events", {"series_ticker": series_ticker, "status": "open", "limit": 100}).get("events", [])
    out = []
    for e in events:
        title = e.get("title", "")
        m = re.match(r"(.+?) vs (.+?): Total Goals", title)
        if not m:
            continue
        home, away = m.group(1).strip(), m.group(2).strip()
        markets = kalshi_get("/markets", {"event_ticker": e["event_ticker"]}).get("markets", [])
        for mk in markets:
            if mk.get("floor_strike") != 2.5 or mk.get("strike_type") != "greater":
                continue
            yes_ask, yes_bid = mk.get("yes_ask_dollars"), mk.get("yes_bid_dollars")
            if yes_ask is None or yes_bid is None:
                continue
            out.append({
                "home": home, "away": away, "ticker": mk["ticker"],
                "yes_ask": float(yes_ask), "yes_bid": float(yes_bid),
            })
    return out


def compute_rolling_p95_bar() -> float:
    """The live selection bar: 95th percentile of the trailing 500
    out-of-fold predictions in the model's own validated history --
    same construction as backtest_season_rolling_percentile.py."""
    df = load_with_xg_player_form_and_shots_venue()
    core_stream = build_stream(df, CORE_CANDIDATES, N_FOLDS_CORE, "core").rename(columns={"pred_p": "pred_p_core"})
    xg_stream = build_stream(df, XG_CANDIDATES, N_FOLDS_XG, "xG")[["fixture_id", "pred_p"]].rename(columns={"pred_p": "pred_p_xg"})
    merged = core_stream.merge(xg_stream, on="fixture_id", how="left")
    merged["pred_p_raw"] = merged["pred_p_xg"].combine_first(merged["pred_p_core"])
    merged["model_used"] = merged["pred_p_xg"].notna().map({True: "xG", False: "core"})
    stream = merged.sort_values("date").reset_index(drop=True)

    calibrators = load_calibrators()
    stream["pred_p"] = apply_calibration(stream["pred_p_raw"], stream["model_used"], calibrators)
    trailing = deque(stream["pred_p"].dropna().tail(500), maxlen=500)
    return float(pd.Series(trailing).quantile(0.95))


def cmd_snapshot(days: int) -> int:
    print("Training the core model...")
    historical = load_with_player_form_and_shots_venue()
    model_df = historical[CORE_CANDIDATES + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X_train = scaler.fit_transform(model_df[CORE_CANDIDATES])
    model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    model.fit(X_train, model_df["over_2_5"])

    print("Training the xG-augmented model...")
    xg_historical = load_with_xg_player_form_and_shots_venue()
    xg_model_df = xg_historical[XG_CANDIDATES + ["over_2_5"]].dropna()
    xg_scaler = StandardScaler()
    X_xg_train = xg_scaler.fit_transform(xg_model_df[XG_CANDIDATES])
    xg_model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    xg_model.fit(X_xg_train, xg_model_df["over_2_5"])

    print("Computing the live rolling-p95 selection bar from trailing historical predictions...")
    bar = compute_rolling_p95_bar()
    print(f"  bar = {bar*100:.1f}%")

    print(f"\nFetching upcoming fixtures across all leagues (next {days} days): {list(LEAGUES)}...")
    upcoming = fetch_upcoming_fixtures(days)
    all_finished = fetch_all_fixtures(None)
    state = replay_to_current_state(all_finished)

    rows = []
    for m in upcoming:
        row = build_feature_row(m, state)
        if row is not None:
            rows.append(row)
    if not rows:
        print("No fixtures had enough team history to score.")
        return 0

    live_df = pd.DataFrame(rows)
    live_df = add_derived_features(live_df)
    live_df = add_xg_derived_features(live_df)
    live_df = add_player_form_derived_features(live_df)
    live_df = add_shots_venue_derived_features(live_df)
    live_df = live_df.dropna(subset=CORE_CANDIDATES)
    if live_df.empty:
        print("All fixtures were missing a required feature.")
        return 0

    has_xg = live_df[XG_RAW_FEATURES + XG_DERIVED_FEATURES].notna().all(axis=1)
    live_df["raw_p"] = pd.NA
    live_df["model_used"] = ""
    core_rows = live_df.loc[~has_xg]
    if not core_rows.empty:
        live_df.loc[~has_xg, "raw_p"] = model.predict_proba(scaler.transform(core_rows[CORE_CANDIDATES]))[:, 1]
        live_df.loc[~has_xg, "model_used"] = "core"
    xg_rows = live_df.loc[has_xg]
    if not xg_rows.empty:
        live_df.loc[has_xg, "raw_p"] = xg_model.predict_proba(xg_scaler.transform(xg_rows[XG_CANDIDATES]))[:, 1]
        live_df.loc[has_xg, "model_used"] = "xG"
    live_df["raw_p"] = live_df["raw_p"].astype(float)

    calibrators = load_calibrators()
    live_df["calibrated_p"] = apply_calibration(live_df["raw_p"], live_df["model_used"], calibrators)

    print("\nFetching real Kalshi prices per league...")
    kalshi_by_comp = {}
    for comp, series in KALSHI_SERIES_BY_COMPETITION.items():
        try:
            markets = fetch_kalshi_over25_for_series(series)
        except Exception as exc:
            print(f"  {comp} ({series}): could not reach Kalshi -- {exc}", file=sys.stderr)
            markets = []
        kalshi_by_comp[comp] = markets
        print(f"  {comp} ({series}): {len(markets)} open 'Over 2.5' markets")

    already_logged = set()
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            already_logged = {row["fixture_id"] for row in csv.DictReader(f)}

    new_rows = []
    for _, r in live_df.iterrows():
        fid = str(int(r["fixture_id"]))
        if fid in already_logged:
            continue
        comp = r["competition"]
        markets = kalshi_by_comp.get(comp, [])
        match = None
        for k in markets:
            if _normalize(k["home"]) == _normalize(r["home_team"]) and _normalize(k["away"]) == _normalize(r["away_team"]):
                match = k
                break
        selected = bool(r["calibrated_p"] >= bar)
        row = {
            "logged_at": pd.Timestamp.utcnow().isoformat(), "fixture_id": fid, "competition": comp,
            "kickoff_date": r["date"], "home_team": r["home_team"], "away_team": r["away_team"],
            "model_used": r["model_used"], "raw_p": round(float(r["raw_p"]), 4),
            "calibrated_p": round(float(r["calibrated_p"]), 4), "rolling_p95_bar": round(bar, 4),
            "selected": selected,
            "kalshi_ticker": match["ticker"] if match else "",
            "kalshi_yes_ask": match["yes_ask"] if match else "",
            "kalshi_yes_bid": match["yes_bid"] if match else "",
            "kalshi_implied_p": round(match["yes_ask"], 4) if match else "",
            "edge_vs_ask": round(float(r["calibrated_p"]) - match["yes_ask"], 4) if match else "",
            "settled": False, "actual_home_goals": "", "actual_away_goals": "",
            "actual_over_2_5": "", "pnl_per_dollar_if_selected": "",
        }
        new_rows.append(row)

    if not new_rows:
        print("\nNothing new to log -- every scoreable fixture is already in the log.")
        return 0

    write_header = not os.path.exists(LOG_PATH)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)

    new_rows.sort(key=lambda r: r["calibrated_p"], reverse=True)
    print(f"\nLogged {len(new_rows)} new fixtures to {LOG_PATH}\n")
    print(f"{'Kickoff':<12}{'Comp':<12}{'Fixture':<38}{'Model P':<10}{'Kalshi':<10}{'Edge':<8}{'Selected'}")
    print("-" * 100)
    for r in new_rows:
        fixture = f"{r['home_team']} vs {r['away_team']}"
        kalshi_str = f"${r['kalshi_yes_ask']:.2f}" if r["kalshi_yes_ask"] != "" else "n/a"
        edge_str = f"{r['edge_vs_ask']*100:+.1f}pp" if r["edge_vs_ask"] != "" else ""
        print(f"{r['kickoff_date']:<12}{r['competition']:<12}{fixture:<38}{r['calibrated_p']*100:5.1f}%   "
              f"{kalshi_str:<10}{edge_str:<8}{'YES' if r['selected'] else ''}")

    n_selected = sum(1 for r in new_rows if r["selected"])
    n_priced = sum(1 for r in new_rows if r["kalshi_yes_ask"] != "")
    print(f"\n{n_selected}/{len(new_rows)} fixtures cleared the rolling-p95 bar ({bar*100:.1f}%). "
          f"{n_priced}/{len(new_rows)} had a real Kalshi price available.")
    return 0


def cmd_settle() -> int:
    if not os.path.exists(LOG_PATH):
        print("No log file yet -- run 'snapshot' first.")
        return 0
    with open(LOG_PATH) as f:
        rows = list(csv.DictReader(f))

    updated = 0
    for row in rows:
        if row["settled"] == "True":
            continue
        try:
            data = apifootball.get("/fixtures", {"id": row["fixture_id"]}, ttl_seconds=300)
        except apifootball.ApiFootballError:
            continue
        resp = data.get("response", [])
        if not resp or resp[0]["fixture"]["status"]["short"] != "FT":
            continue
        home_goals, away_goals = resp[0]["goals"]["home"], resp[0]["goals"]["away"]
        actual_over = (home_goals + away_goals) > 2.5
        row["actual_home_goals"], row["actual_away_goals"] = home_goals, away_goals
        row["actual_over_2_5"] = actual_over
        row["settled"] = True
        if row["selected"] == "True" and row["kalshi_yes_ask"]:
            ask = float(row["kalshi_yes_ask"])
            row["pnl_per_dollar_if_selected"] = round((1 - ask) if actual_over else -ask, 4)
        updated += 1

    if updated:
        with open(LOG_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    print(f"Settled {updated} newly-finished fixture(s).")

    settled_selected = [r for r in rows if r["settled"] == "True" and r["selected"] == "True" and r["pnl_per_dollar_if_selected"]]
    if settled_selected:
        pnl = sum(float(r["pnl_per_dollar_if_selected"]) for r in settled_selected)
        wins = sum(1 for r in settled_selected if r["actual_over_2_5"] == "True")
        print(f"\nRunning forward-test record (selected picks with a real Kalshi price, settled so far):")
        print(f"  n={len(settled_selected)}  wins={wins}  hit rate={wins/len(settled_selected)*100:.1f}%  "
              f"P&L per $1 staked each = {pnl:+.2f}  (ROI={pnl/len(settled_selected)*100:+.1f}%)")
    else:
        print("No settled, selected, priced picks yet -- too early to say anything.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["snapshot", "settle"])
    parser.add_argument("--days", type=int, default=14, help="Snapshot mode: look this many days ahead, default 14")
    args = parser.parse_args()

    if args.mode == "snapshot":
        return cmd_snapshot(args.days)
    return cmd_settle()


if __name__ == "__main__":
    raise SystemExit(main())
