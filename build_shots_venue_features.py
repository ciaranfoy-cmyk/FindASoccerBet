#!/usr/bin/env python3
"""Venue-specific shot-volume features. home_shots_last5 etc. (in
build_dataset_apifootball.py) blend a team's home and away games
together into one rolling average -- same blending issue the xG venue
split (build_xg_venue_features.py) addressed, and shots are the
second-strongest feature tier in the current model (after xG and
player-form), so worth the same treatment: the home team's shot
numbers specifically in their last 5 HOME games, the away team's
specifically in their last 5 AWAY games.

Reuses shot_stats_for()'s already-cached /fixtures/statistics calls
(same endpoint build_dataset_apifootball.py already pulled for every
historical match), so this should be a pure cache replay.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_shots_venue_features.py
"""

import csv
import os
import sys
from collections import defaultdict, deque

import apifootball
from build_dataset_apifootball import fetch_all_fixtures, shot_stats_for

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "shots_venue_features.csv")
ROLLING_N = 5
MIN_GAMES = 3


def rolling_avg(dq: deque, key: str) -> float | None:
    if len(dq) < MIN_GAMES:
        return None
    return sum(x[key] for x in dq) / len(dq)


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches (cache replay, no new API calls expected)")

    # keyed by (team_id, venue) where venue is "home" or "away"
    shot_history: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))

    rows = []
    for i, m in enumerate(matches, start=1):
        try:
            stats = shot_stats_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            stats = {}

        home_id, away_id = m["home_id"], m["away_id"]
        home_key, away_key = (home_id, "home"), (away_id, "away")
        home_hist, away_hist = shot_history[home_key], shot_history[away_key]

        row = {
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": m["competition"], "season": m["season"],
            "home_team": m["home"], "away_team": m["away"],
            "home_venue_shots_last5": rolling_avg(home_hist, "total_shots"),
            "away_venue_shots_last5": rolling_avg(away_hist, "total_shots"),
            "home_venue_shots_on_goal_last5": rolling_avg(home_hist, "shots_on_goal"),
            "away_venue_shots_on_goal_last5": rolling_avg(away_hist, "shots_on_goal"),
            "home_venue_shots_inside_box_last5": rolling_avg(home_hist, "shots_inside_box"),
            "away_venue_shots_inside_box_last5": rolling_avg(away_hist, "shots_inside_box"),
        }
        rows.append(row)

        # Update state AFTER computing this match's pre-match features.
        if home_id in stats:
            shot_history[home_key].append(stats[home_id])
        if away_id in stats:
            shot_history[away_key].append(stats[away_id])

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
