#!/usr/bin/env python3
"""Adds a 10-game rolling xG window alongside the existing 5-game one --
the core (non-xG) model already uses both windows side by side for
goals, and both turned out to matter independently. xG has only ever
had a last-5 version built; this tests whether last-10 adds anything,
the same way it did for goals.

Reuses the cached /fixtures/statistics responses -- no new API calls.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_xg_features_v2.py
"""

import csv
import os
import sys
from collections import defaultdict, deque

import apifootball
from build_dataset_apifootball import fetch_all_fixtures
from build_xg_features import MIN_GAMES_FOR_ROLLING, xg_stats_for

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xg_features_last10.csv")
ROLLING_N_LONG = 10


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

    print(f"Processing {len(matches)} finished matches (cache replay, no new API calls expected)")

    xg_for_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N_LONG))
    xg_against_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N_LONG))

    rows = []
    for i, m in enumerate(matches, start=1):
        try:
            xg = xg_stats_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            xg = {}

        home_id, away_id = m["home_id"], m["away_id"]
        row = {
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "home_xg_last10": rolling_avg(xg_for_history[home_id]),
            "away_xg_last10": rolling_avg(xg_for_history[away_id]),
            "home_xg_against_last10": rolling_avg(xg_against_history[home_id]),
            "away_xg_against_last10": rolling_avg(xg_against_history[away_id]),
        }
        rows.append(row)

        if home_id in xg and away_id in xg:
            home_xg, away_xg = xg[home_id]["xg"], xg[away_id]["xg"]
            xg_for_history[home_id].append(home_xg)
            xg_against_history[home_id].append(away_xg)
            xg_for_history[away_id].append(away_xg)
            xg_against_history[away_id].append(home_xg)

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
