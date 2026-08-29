#!/usr/bin/env python3
"""Build the per-match dataset from api-sports.io (API-Football) instead of
football-data.org, for its much deeper history: Premier League back to
2010-11 (16 seasons) and Championship back to 2011-12 (15 seasons), versus
football-data.org's 3-season free-tier window.

Same no-lookahead feature engineering as build_dataset.py (rolling
goals-for/against, season-to-date, league table position/points/goal-diff
built from a table we track ourselves, rest days, head-to-head), all
consistent within this one provider's team naming — no cross-provider
name-matching needed.

Two-stage design:
  --core-only   Just fixtures + the features above (~30 API calls, fast).
                Use this to validate the pipeline before spending the
                large per-match budget below.
  (default)     Also pulls shot statistics and injuries per match
                (2 extra calls/match) — this is the expensive part
                (~27,000 requests for full history), throttled to stay
                under the 450 requests/minute cap.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_dataset_apifootball.py --core-only
    APIFOOTBALL_KEY=xxxx python3 build_dataset_apifootball.py --seasons 2024 2025
    APIFOOTBALL_KEY=xxxx python3 build_dataset_apifootball.py   # full history, slow
"""

import argparse
import csv
import datetime
import os
import sys
import time
from collections import defaultdict, deque

import apifootball

MIN_PRIOR_GAMES = 5
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "matches_apifootball.csv")

# league_id, first season with real fixture data (empirically confirmed —
# earlier seasons return 0 results, no error, i.e. genuinely no data)
LEAGUES = {
    "PL": {"id": 39, "first_season": 2010},
    "ELC": {"id": 40, "first_season": 2011},
}


def fetch_all_fixtures(seasons_override: list[int] | None) -> list[dict]:
    matches = []
    current_year = datetime.date.today().year if datetime.date.today().month >= 7 else datetime.date.today().year - 1
    for code, info in LEAGUES.items():
        seasons = seasons_override or list(range(info["first_season"], current_year + 1))
        for season in seasons:
            data = apifootball.get("/fixtures", {"league": info["id"], "season": season})
            for m in data.get("response", []):
                if m["fixture"]["status"]["short"] != "FT":
                    continue
                matches.append({
                    "fixture_id": m["fixture"]["id"],
                    "date": m["fixture"]["date"],
                    "competition": code,
                    "season": season,
                    "home": m["teams"]["home"]["name"],
                    "away": m["teams"]["away"]["name"],
                    "home_id": m["teams"]["home"]["id"],
                    "away_id": m["teams"]["away"]["id"],
                    "home_goals": m["goals"]["home"],
                    "away_goals": m["goals"]["away"],
                })
    matches.sort(key=lambda m: m["date"])
    return matches


def rolling_avg(games: deque, n: int, key: str) -> float | None:
    recent = list(games)[-n:]
    if not recent:
        return None
    return sum(g[key] for g in recent) / len(recent)


def season_avg(games: deque, season_label: str, key: str) -> float | None:
    season_games = [g for g in games if g["season_label"] == season_label]
    if not season_games:
        return None
    return sum(g[key] for g in season_games) / len(season_games)


def table_standing(table: dict, season_label: str, team: str) -> tuple:
    season_table = table.get(season_label, {})
    if team not in season_table or season_table[team]["played"] == 0:
        return None, None, None
    ranked = sorted(season_table.values(), key=lambda t: (-t["points"], -(t["gf"] - t["ga"]), -t["gf"]))
    team_row = season_table[team]
    position = next(i for i, t in enumerate(ranked, start=1) if t is team_row)
    return position, team_row["points"], team_row["gf"] - team_row["ga"]


def shot_stats_for(fixture_id: int) -> dict[int, dict]:
    """team_id -> {total_shots, shots_on_goal, shots_inside_box, shots_outside_box}"""
    data = apifootball.get("/fixtures/statistics", {"fixture": fixture_id})
    out = {}
    for team_block in data.get("response", []):
        team_id = team_block["team"]["id"]
        stats = {s["type"]: s["value"] for s in team_block["statistics"]}
        out[team_id] = {
            "total_shots": stats.get("Total Shots"),
            "shots_on_goal": stats.get("Shots on Goal"),
            "shots_inside_box": stats.get("Shots insidebox"),
            "shots_outside_box": stats.get("Shots outsidebox"),
        }
    return out


def injury_count_for(fixture_id: int) -> dict[int, int]:
    """team_id -> count of unique players missing this fixture.

    The API returns exact duplicate entries for the same player in some
    responses (confirmed by inspection), so dedupe by (team, player) id
    before counting rather than counting raw entries.
    """
    data = apifootball.get("/injuries", {"fixture": fixture_id})
    seen: set[tuple[int, int]] = set()
    counts: dict[int, int] = defaultdict(int)
    for entry in data.get("response", []):
        key = (entry["team"]["id"], entry["player"]["id"])
        if key in seen:
            continue
        seen.add(key)
        counts[entry["team"]["id"]] += 1
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--core-only", action="store_true", help="Skip shot-stats/injuries (fast, ~30 calls)")
    parser.add_argument("--seasons", type=int, nargs="+", default=None, help="Restrict to these season start-years instead of full history")
    args = parser.parse_args()

    try:
        matches = fetch_all_fixtures(args.seasons)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Fetched {len(matches)} finished matches across {list(LEAGUES)} "
          f"({'seasons ' + str(args.seasons) if args.seasons else 'full available history'})")

    team_history: dict[str, deque] = defaultdict(lambda: deque())
    team_competition_games: dict[tuple[str, str], int] = defaultdict(int)
    team_last_played: dict[str, datetime.datetime] = {}
    h2h_history: dict[tuple[str, str], list[int]] = defaultdict(list)
    table: dict[str, dict[str, dict]] = defaultdict(dict)
    team_name_to_id: dict[str, int] = {}

    rows = []
    fixture_team_ids: dict[int, tuple[int, int]] = {}
    for m in matches:
        date = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        home, away = m["home"], m["away"]
        home_goals, away_goals = m["home_goals"], m["away_goals"]
        competition = m["competition"]
        season_label = f"{competition}-{m['season']}"
        fixture_team_ids[m["fixture_id"]] = (m["home_id"], m["away_id"])

        home_hist = team_history[home]
        away_hist = team_history[away]
        home_games_played = len(home_hist)
        away_games_played = len(away_hist)

        home_pos, home_pts, home_gd = table_standing(table, season_label, home)
        away_pos, away_pts, away_gd = table_standing(table, season_label, away)

        if home_games_played >= MIN_PRIOR_GAMES and away_games_played >= MIN_PRIOR_GAMES:
            home_rest = (date - team_last_played[home]).days if home in team_last_played else None
            away_rest = (date - team_last_played[away]).days if away in team_last_played else None
            pair_key = tuple(sorted([home, away]))
            h2h_goals = h2h_history[pair_key]

            rows.append({
                "fixture_id": m["fixture_id"],
                "date": date.date().isoformat(),
                "competition": competition,
                "season": m["season"],
                "home_team": home,
                "away_team": away,
                "home_games_played": home_games_played,
                "away_games_played": away_games_played,
                "home_competition_games": team_competition_games[(home, competition)],
                "away_competition_games": team_competition_games[(away, competition)],
                "home_gf_last5": rolling_avg(home_hist, 5, "gf"),
                "home_ga_last5": rolling_avg(home_hist, 5, "ga"),
                "away_gf_last5": rolling_avg(away_hist, 5, "gf"),
                "away_ga_last5": rolling_avg(away_hist, 5, "ga"),
                "home_gf_last10": rolling_avg(home_hist, 10, "gf"),
                "home_ga_last10": rolling_avg(home_hist, 10, "ga"),
                "away_gf_last10": rolling_avg(away_hist, 10, "gf"),
                "away_ga_last10": rolling_avg(away_hist, 10, "ga"),
                "home_gf_season": season_avg(home_hist, season_label, "gf"),
                "home_ga_season": season_avg(home_hist, season_label, "ga"),
                "away_gf_season": season_avg(away_hist, season_label, "gf"),
                "away_ga_season": season_avg(away_hist, season_label, "ga"),
                "home_league_position": home_pos,
                "away_league_position": away_pos,
                "home_points": home_pts,
                "away_points": away_pts,
                "home_goal_diff": home_gd,
                "away_goal_diff": away_gd,
                "home_rest_days": home_rest,
                "away_rest_days": away_rest,
                "h2h_games": len(h2h_goals),
                "h2h_avg_goals": (sum(h2h_goals) / len(h2h_goals)) if h2h_goals else None,
                "total_goals": home_goals + away_goals,
                "over_2_5": int((home_goals + away_goals) > 2.5),
            })

        home_hist.append({"gf": home_goals, "ga": away_goals, "season_label": season_label})
        away_hist.append({"gf": away_goals, "ga": home_goals, "season_label": season_label})
        team_competition_games[(home, competition)] += 1
        team_competition_games[(away, competition)] += 1
        team_last_played[home] = date
        team_last_played[away] = date
        h2h_history[tuple(sorted([home, away]))].append(home_goals + away_goals)

        season_table = table[season_label]
        home_row = season_table.setdefault(home, {"points": 0, "played": 0, "gf": 0, "ga": 0})
        away_row = season_table.setdefault(away, {"points": 0, "played": 0, "gf": 0, "ga": 0})
        home_row["played"] += 1
        away_row["played"] += 1
        home_row["gf"] += home_goals
        home_row["ga"] += away_goals
        away_row["gf"] += away_goals
        away_row["ga"] += home_goals
        if home_goals > away_goals:
            home_row["points"] += 3
        elif away_goals > home_goals:
            away_row["points"] += 3
        else:
            home_row["points"] += 1
            away_row["points"] += 1

    print(f"Built {len(rows)} feature rows (dropped {len(matches) - len(rows)} early-history games)")

    if not args.core_only:
        print(f"Pulling shot statistics + injuries for {len(rows)} matches "
              f"({len(rows) * 2} requests, throttled under 450/min)...")
        fixture_ids_by_row = {r["fixture_id"]: r for r in rows}
        for i, (fixture_id, row) in enumerate(fixture_ids_by_row.items(), start=1):
            try:
                shots = shot_stats_for(fixture_id)
                injuries = injury_count_for(fixture_id)
            except apifootball.ApiFootballError as exc:
                print(f"  [{i}/{len(rows)}] fixture {fixture_id}: {exc}", file=sys.stderr)
                continue

            home_id, away_id = fixture_team_ids[fixture_id]
            if home_id in shots:
                row["home_total_shots"] = shots[home_id]["total_shots"]
                row["home_shots_on_goal"] = shots[home_id]["shots_on_goal"]
                row["home_shots_inside_box"] = shots[home_id]["shots_inside_box"]
                row["home_shots_outside_box"] = shots[home_id]["shots_outside_box"]
            if away_id in shots:
                row["away_total_shots"] = shots[away_id]["total_shots"]
                row["away_shots_on_goal"] = shots[away_id]["shots_on_goal"]
                row["away_shots_inside_box"] = shots[away_id]["shots_inside_box"]
                row["away_shots_outside_box"] = shots[away_id]["shots_outside_box"]

            row["home_missing_players"] = injuries.get(home_id, 0)
            row["away_missing_players"] = injuries.get(away_id, 0)

            if i % 200 == 0:
                print(f"  ...{i}/{len(rows)}")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    fieldnames = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)
    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
