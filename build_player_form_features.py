#!/usr/bin/env python3
"""Build a "current attacking form" feature: for each match, sum the
rolling goals-per-start (over each player's own last N starts, not
career average) of TODAY's starting forwards/attacking-mids — a genuine
hot/cold signal for whoever's actually selected, as opposed to
attacking_output_missing (build_lineup_features.py), which only fires
when a key player is absent and used a stale career average.

Reuses the exact same lineup + goal-event data already pulled for
build_lineup_features.py — apifootball.get() caches by fixture_id, so
this is a cache-hit replay, no new API calls, should run in minutes.

Single chronological pass, same no-lookahead discipline as everything
else: a match's feature is computed from player history BEFORE that
match, then that match's own goals are folded into the history after.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_player_form_features.py
"""

import os
import sys
from collections import defaultdict, deque

import apifootball
from build_dataset_apifootball import fetch_all_fixtures
from build_lineup_features import goal_scorers_for, lineup_for

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "player_form_features.csv")
ATTACKING_POS = {"F", "M"}
ROLLING_WINDOW = 10       # last N starts used for each player's rolling form
MIN_STARTS_FOR_FORM = 3   # need at least this many tracked prior starts to trust a player's number


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches (cache replay, no new API calls expected)")

    player_goal_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    player_positions: dict[int, str] = {}

    rows = []
    for i, m in enumerate(matches, start=1):
        try:
            lineups = lineup_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            lineups = {}

        row = {
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": m["competition"], "season": m["season"],
            "home_team": m["home"], "away_team": m["away"],
        }

        side_lineups = [("home", m["home_id"], lineups.get(m["home_id"])),
                         ("away", m["away_id"], lineups.get(m["away_id"]))]

        for side, team_id, lineup in side_lineups:
            if not lineup:
                row[f"{side}_attacking_form"] = None
                row[f"{side}_attacking_form_n_players"] = 0
                continue

            attackers = [(pid, pos) for pid, pos, name in lineup if pos in ATTACKING_POS]
            if not attackers:
                row[f"{side}_attacking_form"] = None
                row[f"{side}_attacking_form_n_players"] = 0
                continue

            form_values = []
            for pid, pos in attackers:
                hist = player_goal_history.get(pid)
                if hist and len(hist) >= MIN_STARTS_FOR_FORM:
                    form_values.append(sum(hist) / len(hist))

            # Require full coverage (every starting attacker has enough
            # tracked history) so the sum isn't silently biased downward
            # by players we just don't have data for yet.
            if len(form_values) == len(attackers):
                row[f"{side}_attacking_form"] = round(sum(form_values), 4)
                row[f"{side}_attacking_form_n_players"] = len(form_values)
            else:
                row[f"{side}_attacking_form"] = None
                row[f"{side}_attacking_form_n_players"] = len(form_values)

        rows.append(row)

        # Update state AFTER computing this match's pre-match feature.
        try:
            scorers = goal_scorers_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            scorers = []
        goals_this_match: dict[int, int] = defaultdict(int)
        for team_id, pid in scorers:
            goals_this_match[pid] += 1

        for side, team_id, lineup in side_lineups:
            if not lineup:
                continue
            for pid, pos, name in lineup:
                player_positions[pid] = pos
                if pos in ATTACKING_POS:
                    player_goal_history[pid].append(goals_this_match.get(pid, 0))

        if i % 2000 == 0:
            print(f"  ...{i}/{len(matches)}")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    import csv
    fieldnames = list(rows[0].keys())
    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
