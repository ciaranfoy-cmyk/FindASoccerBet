#!/usr/bin/env python3
"""Venue-specific ACTUAL GOALS features -- the goals equivalent of
build_shots_venue_features.py, built after a real, checked pattern: Man
City's home games specifically have gone over 2.5 in 28/38 (74%) of their
last two full PL seasons at the Etihad, well above their any-venue rate
(~55% over the same run of games). Every goals feature that currently
exists (home_gf_last5, home_gf_season, etc. in build_dataset_apifootball.py)
blends a team's home and away games together -- exactly the blending
issue build_shots_venue_features.py already fixed for shots, just never
fixed for the actual goals themselves. The model currently has no direct
feature for "how high-scoring are this team's games specifically at this
venue" -- only a venue split for shot volume and a goals-per-shot
conversion rate, neither of which is the same signal.

Same discipline as build_shots_venue_features.py: keyed by (team_id,
venue), last ROLLING_N games specifically at that venue, equal weight
(deliberately NOT recency-weighted like build_xg_weighted_features.py --
this tests a different, orthogonal hypothesis: does venue-specific
windowing add signal on its own, kept simple so it's a fair like-for-like
comparison against the existing shots-venue precedent that already
validated this exact windowing style). No new API calls -- reuses the
per-match goals already present on every fetch_all_fixtures() row.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_goals_venue_features.py
"""

import csv
import os
import sys
from collections import defaultdict, deque

import apifootball
from build_dataset_apifootball import fetch_all_fixtures

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "goals_venue_features.csv")
ROLLING_N = 5
MIN_GAMES = 3


def rolling_avg(dq: deque, key: str) -> float | None:
    if len(dq) < MIN_GAMES:
        return None
    return sum(x[key] for x in dq) / len(dq)


def rolling_over_pct(dq: deque) -> float | None:
    if len(dq) < MIN_GAMES:
        return None
    return sum(1 for x in dq if x["total"] > 2.5) / len(dq)


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches (pure replay, no new API calls)")

    # keyed by (team_id, venue) where venue is "home" or "away"
    goals_history: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))

    rows = []
    for i, m in enumerate(matches, start=1):
        home_id, away_id = m["home_id"], m["away_id"]
        home_key, away_key = (home_id, "home"), (away_id, "away")
        home_hist, away_hist = goals_history[home_key], goals_history[away_key]

        row = {
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": m["competition"], "season": m["season"],
            "home_team": m["home"], "away_team": m["away"],
            "home_venue_gf_last5": rolling_avg(home_hist, "gf"),
            "home_venue_ga_last5": rolling_avg(home_hist, "ga"),
            "home_venue_total_last5": rolling_avg(home_hist, "total"),
            "home_venue_over_pct_last5": rolling_over_pct(home_hist),
            "away_venue_gf_last5": rolling_avg(away_hist, "gf"),
            "away_venue_ga_last5": rolling_avg(away_hist, "ga"),
            "away_venue_total_last5": rolling_avg(away_hist, "total"),
            "away_venue_over_pct_last5": rolling_over_pct(away_hist),
        }
        rows.append(row)

        # Update state AFTER computing this match's pre-match features.
        home_goals, away_goals = m["home_goals"], m["away_goals"]
        total = home_goals + away_goals
        goals_history[home_key].append({"gf": home_goals, "ga": away_goals, "total": total})
        goals_history[away_key].append({"gf": away_goals, "ga": home_goals, "total": total})

        if i % 5000 == 0:
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
