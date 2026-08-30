#!/usr/bin/env python3
"""Build lineup/player-level features for every match in the historical
fixture list: attacking-output missing, defensive-regulars missing, and
lineup disruption/newness — a single chronological pass, same
no-lookahead discipline as build_dataset_apifootball.py, but tracking
individual players instead of just team aggregates.

For each match, BEFORE folding in that match's own lineup/goals, we use
only what's been tracked so far to compute:
  - {side}_lineup_change_count: how many of the team's "usual XI" (most-
    started 11 over the trailing window) are missing from today's XI.
  - {side}_new_signings_count: players in today's XI with zero prior
    tracked starts for this team.
  - {side}_attacking_output_missing: sum of missing usual-XI forwards/
    attacking-mids' individual goals-per-start (a high-scoring striker
    missing counts far more than a fringe player missing).
  - {side}_defensive_regulars_missing: count of missing usual-XI
    defenders/goalkeepers, weighted by how large a share of the team's
    starts they'd normally take (a proxy for "how first-choice" — there's
    no clean per-defender goals-prevented number the way there is for
    attackers, so this is deliberately a weaker, different kind of proxy).

Writes a separate CSV keyed by fixture_id, meant to be merged into
data/matches_apifootball.csv (via fixture_id) once validated — kept
separate so the existing modeling pipeline isn't touched until this is
actually shown to add signal.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_lineup_features.py
    APIFOOTBALL_KEY=xxxx python3 build_lineup_features.py --seasons 2024 2025
"""

import argparse
import csv
import os
import sys
from collections import defaultdict, deque

import apifootball
from build_dataset_apifootball import fetch_all_fixtures

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lineup_features.csv")
USUAL_XI_WINDOW = 15   # trailing starts used to define a team's "usual" XI
USUAL_XI_MIN_HISTORY = 8  # need at least this many tracked lineups before trusting "usual XI"
ATTACKING_POS = {"F", "M"}   # forwards + attacking mids treated as attacking output
DEFENSIVE_POS = {"D", "G"}   # defenders + goalkeeper


def lineup_for(fixture_id: int) -> dict[int, list[tuple[int, str, str]]]:
    """team_id -> [(player_id, position, name), ...] for the starting XI."""
    data = apifootball.get("/fixtures/lineups", {"fixture": fixture_id})
    out = {}
    for team_block in data.get("response", []):
        team_id = team_block["team"]["id"]
        starters = []
        for p in team_block.get("startXI", []):
            player = p.get("player", {})
            pid = player.get("id")
            pos = player.get("pos")
            name = player.get("name")
            if pid is not None and pos:
                starters.append((pid, pos, name))
        if starters:
            out[team_id] = starters
    return out


def goal_scorers_for(fixture_id: int) -> list[tuple[int, int]]:
    """[(team_id, player_id), ...] for non-own-goal goals in the match."""
    data = apifootball.get("/fixtures/events", {"fixture": fixture_id})
    scorers = []
    for e in data.get("response", []):
        if e.get("type") != "Goal" or e.get("detail") == "Missed Penalty" or e.get("detail") == "Own Goal":
            continue
        player = e.get("player") or {}
        pid = player.get("id")
        if pid is not None:
            scorers.append((e["team"]["id"], pid))
    return scorers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seasons", type=int, nargs="+", default=None, help="Restrict to these season start-years")
    args = parser.parse_args()

    try:
        matches = fetch_all_fixtures(args.seasons)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches "
          f"({'seasons ' + str(args.seasons) if args.seasons else 'full available history'})")

    team_lineup_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=USUAL_XI_WINDOW))
    player_starts: dict[tuple[int, int], int] = defaultdict(int)   # (team_id, player_id) -> starts
    player_positions: dict[int, str] = {}                          # player_id -> last known position
    player_goals: dict[int, int] = defaultdict(int)                # player_id -> cumulative goals (global)

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
                for key in ("lineup_change_count", "new_signings_count",
                            "attacking_output_missing", "defensive_regulars_missing"):
                    row[f"{side}_{key}"] = None
                continue

            starter_ids = {pid for pid, pos, name in lineup}
            hist = team_lineup_history[team_id]

            if len(hist) >= USUAL_XI_MIN_HISTORY:
                freq = defaultdict(int)
                for past_xi in hist:
                    for pid in past_xi:
                        freq[pid] += 1
                usual_xi = set(sorted(freq, key=lambda p: -freq[p])[:11])
                missing = usual_xi - starter_ids

                attacking_missing = 0.0
                defensive_missing = 0.0
                for pid in missing:
                    pos = player_positions.get(pid)
                    starts = player_starts.get((team_id, pid), 0)
                    if pos in ATTACKING_POS:
                        goals_per_start = player_goals.get(pid, 0) / starts if starts > 0 else 0.0
                        attacking_missing += goals_per_start
                    elif pos in DEFENSIVE_POS:
                        start_share = starts / len(hist) if len(hist) > 0 else 0.0
                        defensive_missing += start_share

                row[f"{side}_lineup_change_count"] = len(missing)
                row[f"{side}_attacking_output_missing"] = round(attacking_missing, 4)
                row[f"{side}_defensive_regulars_missing"] = round(defensive_missing, 4)
            else:
                row[f"{side}_lineup_change_count"] = None
                row[f"{side}_attacking_output_missing"] = None
                row[f"{side}_defensive_regulars_missing"] = None

            new_signings = sum(1 for pid in starter_ids if player_starts.get((team_id, pid), 0) == 0)
            row[f"{side}_new_signings_count"] = new_signings

        rows.append(row)

        # Update state AFTER computing this match's pre-match features.
        for side, team_id, lineup in side_lineups:
            if not lineup:
                continue
            starter_ids = {pid for pid, pos, name in lineup}
            team_lineup_history[team_id].append(starter_ids)
            for pid, pos, name in lineup:
                player_starts[(team_id, pid)] += 1
                player_positions[pid] = pos

        try:
            scorers = goal_scorers_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            scorers = []
        for team_id, pid in scorers:
            player_goals[pid] += 1

        if i % 1000 == 0:
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
