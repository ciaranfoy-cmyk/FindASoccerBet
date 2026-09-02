#!/usr/bin/env python3
"""Build the per-match dataset from api-sports.io (API-Football) instead of
football-data.org, for its much deeper history: Premier League back to
2010-11 (16 seasons) and Championship back to 2011-12 (15 seasons), versus
football-data.org's 3-season free-tier window.

Same no-lookahead feature engineering as build_dataset.py (rolling
goals-for/against, season-to-date, league table position/points/goal-diff
built from a table we track ourselves, rest days, head-to-head), all
consistent within this one provider's team naming — no cross-provider
name-matching needed. Also adds, from shot statistics and injuries data
football-data.org doesn't offer at all:

  - Rolling shot volume/quality (total shots, shots on goal, shots inside
    the box) over each team's last 5 games WITH shot data available —
    shot-tracking coverage is only ~72% of matches, so "last 5" here
    means last 5 games with data, not strictly the last 5 calendar games.
  - Clean-sheet percentage over the last 5/10 games (derived from goals
    already tracked, no extra data needed).
  - A team-level shot-conversion rate (goals per shot) over the last 5.
  - Missing-player count for the match itself (not rolled — this is
    same-game team-news information, not history).

Two-stage design:
  --core-only   Skip shot-stats/injuries entirely (~30 API calls, fast).
                Use this to validate the pipeline before spending the
                large per-match budget below.
  (default)     Also pulls shot statistics and injuries per match
                (2 extra calls/match) — this is the expensive part
                (~27,000 requests for full history), throttled to stay
                under the 450 requests/minute cap. Already-cached
                responses (e.g. from a prior run) cost nothing to replay.

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
from collections import defaultdict, deque

import apifootball

MIN_PRIOR_GAMES = 5
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "matches_apifootball.csv")

# league_id, first season to pull. For PL/ELC this is the earliest season
# with real fixture data at all (empirically confirmed -- earlier seasons
# return 0 results, no error). For newly-added leagues it's a deliberately
# shorter window: rolling_validation-style testing on the EPL/Championship
# core model found identical performance (138/219 = 63.0% both ways)
# training on ~6.5 years of history vs. the full ~11 -- the extra years
# add no detectable value, so new leagues start at a matching ~7-season
# depth instead of each league's own shot-stat-coverage start.
LEAGUES = {
    "PL": {"id": 39, "first_season": 2010},
    "ELC": {"id": 40, "first_season": 2011},
    "LALIGA": {"id": 140, "first_season": 2019},
    "BUNDESLIGA": {"id": 78, "first_season": 2019},
    "SERIEA": {"id": 135, "first_season": 2019},
    "LIGUE1": {"id": 61, "first_season": 2019},
    "MLS": {"id": 253, "first_season": 2019},
    "EREDIVISIE": {"id": 88, "first_season": 2019},
    "SUPERLIG": {"id": 203, "first_season": 2019},
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


def clean_sheet_pct(games: deque, n: int) -> float | None:
    recent = list(games)[-n:]
    if not recent:
        return None
    return sum(1 for g in recent if g["ga"] == 0) / len(recent)


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


def safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def shot_stats_for(fixture_id: int) -> dict[int, dict]:
    """team_id -> {total_shots, shots_on_goal, shots_inside_box, shots_outside_box}"""
    data = apifootball.get("/fixtures/statistics", {"fixture": fixture_id})
    out = {}
    for team_block in data.get("response", []):
        team_id = team_block["team"]["id"]
        stats = {s["type"]: s["value"] for s in team_block["statistics"]}
        values = {
            "total_shots": stats.get("Total Shots"),
            "shots_on_goal": stats.get("Shots on Goal"),
            "shots_inside_box": stats.get("Shots insidebox"),
            "shots_outside_box": stats.get("Shots outsidebox"),
        }
        if all(v is not None for v in values.values()):
            out[team_id] = values
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
    team_shot_history: dict[str, deque] = defaultdict(lambda: deque())
    team_competition_games: dict[tuple[str, str], int] = defaultdict(int)
    team_last_played: dict[str, datetime.datetime] = {}
    h2h_history: dict[tuple[str, str], list[int]] = defaultdict(list)
    table: dict[str, dict[str, dict]] = defaultdict(dict)

    rows = []
    for i, m in enumerate(matches, start=1):
        date = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        home, away = m["home"], m["away"]
        home_goals, away_goals = m["home_goals"], m["away_goals"]
        competition = m["competition"]
        season_label = f"{competition}-{m['season']}"

        home_hist = team_history[home]
        away_hist = team_history[away]
        home_shot_hist = team_shot_history[home]
        away_shot_hist = team_shot_history[away]
        home_games_played = len(home_hist)
        away_games_played = len(away_hist)

        home_pos, home_pts, home_gd = table_standing(table, season_label, home)
        away_pos, away_pts, away_gd = table_standing(table, season_label, away)

        home_shots_this = away_shots_this = None
        home_missing = away_missing = None
        if not args.core_only:
            try:
                shots = shot_stats_for(m["fixture_id"])
                injuries = injury_count_for(m["fixture_id"])
            except apifootball.ApiFootballError as exc:
                print(f"  [{i}/{len(matches)}] fixture {m['fixture_id']}: {exc}", file=sys.stderr)
                shots, injuries = {}, {}
            home_shots_this = shots.get(m["home_id"])
            away_shots_this = shots.get(m["away_id"])
            home_missing = injuries.get(m["home_id"], 0)
            away_missing = injuries.get(m["away_id"], 0)

        if home_games_played >= MIN_PRIOR_GAMES and away_games_played >= MIN_PRIOR_GAMES:
            home_rest = (date - team_last_played[home]).days if home in team_last_played else None
            away_rest = (date - team_last_played[away]).days if away in team_last_played else None
            pair_key = tuple(sorted([home, away]))
            h2h_goals = h2h_history[pair_key]

            home_shots_last5 = rolling_avg(home_shot_hist, 5, "total_shots")
            away_shots_last5 = rolling_avg(away_shot_hist, 5, "total_shots")

            row = {
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
                "home_clean_sheet_pct_last5": clean_sheet_pct(home_hist, 5),
                "away_clean_sheet_pct_last5": clean_sheet_pct(away_hist, 5),
                "home_clean_sheet_pct_last10": clean_sheet_pct(home_hist, 10),
                "away_clean_sheet_pct_last10": clean_sheet_pct(away_hist, 10),
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
            }

            if not args.core_only:
                row.update({
                    "home_shots_last5": home_shots_last5,
                    "home_shots_on_goal_last5": rolling_avg(home_shot_hist, 5, "shots_on_goal"),
                    "home_shots_inside_box_last5": rolling_avg(home_shot_hist, 5, "shots_inside_box"),
                    "away_shots_last5": away_shots_last5,
                    "away_shots_on_goal_last5": rolling_avg(away_shot_hist, 5, "shots_on_goal"),
                    "away_shots_inside_box_last5": rolling_avg(away_shot_hist, 5, "shots_inside_box"),
                    "home_conversion_rate_last5": safe_div(rolling_avg(home_hist, 5, "gf"), home_shots_last5),
                    "away_conversion_rate_last5": safe_div(rolling_avg(away_hist, 5, "gf"), away_shots_last5),
                    "home_total_shots": home_shots_this["total_shots"] if home_shots_this else None,
                    "home_shots_on_goal": home_shots_this["shots_on_goal"] if home_shots_this else None,
                    "home_shots_inside_box": home_shots_this["shots_inside_box"] if home_shots_this else None,
                    "home_shots_outside_box": home_shots_this["shots_outside_box"] if home_shots_this else None,
                    "away_total_shots": away_shots_this["total_shots"] if away_shots_this else None,
                    "away_shots_on_goal": away_shots_this["shots_on_goal"] if away_shots_this else None,
                    "away_shots_inside_box": away_shots_this["shots_inside_box"] if away_shots_this else None,
                    "away_shots_outside_box": away_shots_this["shots_outside_box"] if away_shots_this else None,
                    "home_missing_players": home_missing,
                    "away_missing_players": away_missing,
                })

            rows.append(row)

        home_hist.append({"gf": home_goals, "ga": away_goals, "season_label": season_label})
        away_hist.append({"gf": away_goals, "ga": home_goals, "season_label": season_label})
        if home_shots_this:
            home_shot_hist.append(home_shots_this)
        if away_shots_this:
            away_shot_hist.append(away_shots_this)
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

        if not args.core_only and i % 1000 == 0:
            print(f"  ...{i}/{len(matches)}")

    print(f"Built {len(rows)} feature rows (dropped {len(matches) - len(rows)} early-history games)")

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
