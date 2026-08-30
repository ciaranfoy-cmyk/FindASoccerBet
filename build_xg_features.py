#!/usr/bin/env python3
"""Build real xG-based features from the `expected_goals` field inside
/fixtures/statistics — present for PL from 2022-23 and Championship from
2023-24 onward (confirmed empirically; earlier seasons don't carry it at
all). No new API calls needed: we already fetched /fixtures/statistics
for every match while building shot stats, so this is a local re-parse
of the existing cache.

Same no-lookahead discipline as everything else: for each match, rolling
xG-for/xG-against are computed from each team's last N games WITH xG
data available (not strictly calendar-last-N, since coverage only spans
recent seasons), then that match's own xG is folded in afterward.

Also builds a "finishing over/underperformance" feature — actual goals
minus xG, rolled forward — a genuine regression-to-mean signal that
wasn't previously representable (goals-for alone can't distinguish a
team converting normally from one running hot or cold relative to the
quality of chances they've actually created).

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_xg_features.py
"""

import csv
import os
import sys
from collections import defaultdict, deque

import apifootball
from build_dataset_apifootball import fetch_all_fixtures

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xg_features.csv")
ROLLING_N = 5
MIN_GAMES_FOR_ROLLING = 3


def xg_stats_for(fixture_id: int) -> dict[int, dict]:
    """team_id -> {"xg": float, "goals_prevented": float or None}, only for teams where expected_goals is present."""
    data = apifootball.get("/fixtures/statistics", {"fixture": fixture_id})
    out = {}
    for team_block in data.get("response", []):
        team_id = team_block["team"]["id"]
        stats = {s["type"]: s["value"] for s in team_block["statistics"]}
        xg = stats.get("expected_goals")
        if xg is not None:
            out[team_id] = {"xg": float(xg), "goals_prevented": stats.get("goals_prevented")}
    return out


def rolling_avg(dq: deque) -> float | None:
    if len(dq) < MIN_GAMES_FOR_ROLLING:
        return None
    return sum(dq) / len(dq)


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches (re-parsing cached /fixtures/statistics)")

    xg_for_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))
    xg_against_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))
    finishing_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))  # actual goals - xG, per game

    rows = []
    for i, m in enumerate(matches, start=1):
        try:
            xg = xg_stats_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            xg = {}

        home_id, away_id = m["home_id"], m["away_id"]
        row = {
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": m["competition"], "season": m["season"],
            "home_team": m["home"], "away_team": m["away"],
            "home_xg_last5": rolling_avg(xg_for_history[home_id]),
            "away_xg_last5": rolling_avg(xg_for_history[away_id]),
            "home_xg_against_last5": rolling_avg(xg_against_history[home_id]),
            "away_xg_against_last5": rolling_avg(xg_against_history[away_id]),
            "home_finishing_last5": rolling_avg(finishing_history[home_id]),
            "away_finishing_last5": rolling_avg(finishing_history[away_id]),
            "home_xg_this_match": xg.get(home_id, {}).get("xg"),
            "away_xg_this_match": xg.get(away_id, {}).get("xg"),
        }
        rows.append(row)

        # Update state AFTER computing this match's pre-match features.
        if home_id in xg and away_id in xg:
            home_xg, away_xg = xg[home_id]["xg"], xg[away_id]["xg"]
            xg_for_history[home_id].append(home_xg)
            xg_against_history[home_id].append(away_xg)
            xg_for_history[away_id].append(away_xg)
            xg_against_history[away_id].append(home_xg)
            finishing_history[home_id].append(m["home_goals"] - home_xg)
            finishing_history[away_id].append(m["away_goals"] - away_xg)

        if i % 2000 == 0:
            print(f"  ...{i}/{len(matches)}")

    n_with_xg = sum(1 for r in rows if r["home_xg_this_match"] is not None)
    print(f"{n_with_xg} matches have real xG for this match itself (leakage-only sanity field, not a predictor)")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
