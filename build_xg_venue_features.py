#!/usr/bin/env python3
"""Two refinements on the real-xG features, both from data already
cached (no new API calls):

1. Venue-specific xG: right now home_xg_last5/away_xg_last5 blend a
   team's home and away games together. This splits them -- the home
   team's xG in their last 5 HOME games specifically, the away team's
   xG in their last 5 AWAY games specifically -- since home-advantage
   effects can differ meaningfully by venue.

2. xG per shot (shot quality, not just volume): tracks each team's own
   (xg, shots) pairs per game so the ratio is computed over the exact
   same set of games for both quantities (dividing the existing
   home_xg_last5 by home_shots_last5 would be wrong -- those two rolling
   averages are built from different underlying game-sets, since shot
   coverage goes back much further than real xG coverage).

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_xg_venue_features.py
"""

import csv
import os
import sys
from collections import defaultdict, deque

import apifootball
from build_dataset_apifootball import fetch_all_fixtures

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xg_venue_features.csv")
ROLLING_N = 5
MIN_GAMES = 3


def xg_and_shots_for(fixture_id: int) -> dict[int, dict]:
    """team_id -> {"xg": float, "shots": float}, only for teams with both fields present."""
    data = apifootball.get("/fixtures/statistics", {"fixture": fixture_id})
    out = {}
    for team_block in data.get("response", []):
        team_id = team_block["team"]["id"]
        stats = {s["type"]: s["value"] for s in team_block["statistics"]}
        xg = stats.get("expected_goals")
        shots = stats.get("Total Shots")
        if xg is not None and shots is not None and shots > 0:
            out[team_id] = {"xg": float(xg), "shots": float(shots)}
    return out


def rolling_avg(dq: deque, key: str | None = None) -> float | None:
    if len(dq) < MIN_GAMES:
        return None
    if key is None:
        return sum(dq) / len(dq)
    return sum(x[key] for x in dq) / len(dq)


def rolling_ratio(dq: deque) -> float | None:
    """Sum(xg)/sum(shots) over the window -- more stable than averaging
    per-game ratios, which get noisy when a single game has few shots."""
    if len(dq) < MIN_GAMES:
        return None
    total_xg = sum(x["xg"] for x in dq)
    total_shots = sum(x["shots"] for x in dq)
    return total_xg / total_shots if total_shots > 0 else None


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches (cache replay, no new API calls expected)")

    # keyed by (team_id, venue) where venue is "home" or "away"
    xg_for_history: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))
    xg_against_history: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))

    rows = []
    for i, m in enumerate(matches, start=1):
        try:
            stats = xg_and_shots_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            stats = {}

        home_id, away_id = m["home_id"], m["away_id"]
        home_key, away_key = (home_id, "home"), (away_id, "away")

        home_hist = xg_for_history[home_key]
        away_hist = xg_for_history[away_key]
        home_against_hist = xg_against_history[home_key]
        away_against_hist = xg_against_history[away_key]

        row = {
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": m["competition"], "season": m["season"],
            "home_team": m["home"], "away_team": m["away"],
            "home_venue_xg_last5": rolling_avg(home_hist, "xg"),
            "away_venue_xg_last5": rolling_avg(away_hist, "xg"),
            "home_venue_xg_against_last5": rolling_avg(home_against_hist),
            "away_venue_xg_against_last5": rolling_avg(away_against_hist),
            "home_xg_per_shot_last5": rolling_ratio(home_hist),
            "away_xg_per_shot_last5": rolling_ratio(away_hist),
        }
        rows.append(row)

        # Update state AFTER computing this match's pre-match features.
        if home_id in stats and away_id in stats:
            xg_for_history[home_key].append({"xg": stats[home_id]["xg"], "shots": stats[home_id]["shots"]})
            xg_for_history[away_key].append({"xg": stats[away_id]["xg"], "shots": stats[away_id]["shots"]})
            xg_against_history[home_key].append(stats[away_id]["xg"])
            xg_against_history[away_key].append(stats[home_id]["xg"])

        if i % 2000 == 0:
            print(f"  ...{i}/{len(matches)}")

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
