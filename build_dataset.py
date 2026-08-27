#!/usr/bin/env python3
"""Build a per-match dataset for over/under-2.5-goals analysis.

Pulls every finished Premier League + Championship match available on the
free tier (2023-24 through the in-progress 2026-27 season) and, for each
match, computes pre-match features from ONLY what happened before that
match — no lookahead. A team's rolling stats carry over across divisions
(promotion/relegation) and across seasons, since they're built from the
actual match-by-match log, not season-standings snapshots.

Output: data/matches.csv, one row per match with:
  date, competition, matchday, home_team, away_team,
  home_games_played, away_games_played          (career games tracked so far)
  home_competition_games, away_competition_games (games in *this* competition so far)
  home_gf_last5, home_ga_last5, away_gf_last5, away_ga_last5
  home_gf_last10, home_ga_last10, away_gf_last10, away_ga_last10
  home_gf_season, home_ga_season, away_gf_season, away_ga_season
  home_rest_days, away_rest_days
  h2h_games, h2h_avg_goals
  total_goals, over_2_5                          (the outcome)

Only rows where both teams already have MIN_PRIOR_GAMES of tracked
history are kept, so the rolling features are meaningful.

Usage:
    FOOTBALL_DATA_API_KEY=xxxx python3 build_dataset.py
"""

import csv
import datetime
import os
import sys
from collections import defaultdict, deque

import football_data

SEASONS = [2023, 2024, 2025, 2026]
COMPETITIONS = ["PL", "ELC"]
MIN_PRIOR_GAMES = 5
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "matches.csv")


def fetch_all_matches() -> list[dict]:
    matches = []
    for competition in COMPETITIONS:
        for season in SEASONS:
            ttl = None if season < 2026 else 0  # completed seasons cache forever; current season always fresh
            data = football_data.get(
                f"/competitions/{competition}/matches", {"season": season}, ttl_seconds=ttl
            )
            for m in data.get("matches", []):
                if m["status"] != "FINISHED":
                    continue
                matches.append({
                    "date": m["utcDate"],
                    "competition": competition,
                    "matchday": m["matchday"],
                    "home": m["homeTeam"]["name"],
                    "away": m["awayTeam"]["name"],
                    "home_goals": m["score"]["fullTime"]["home"],
                    "away_goals": m["score"]["fullTime"]["away"],
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


def main() -> int:
    try:
        matches = fetch_all_matches()
    except football_data.FootballDataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Fetched {len(matches)} finished matches across {COMPETITIONS} x {SEASONS}")

    team_history: dict[str, deque] = defaultdict(lambda: deque())
    team_competition_games: dict[tuple[str, str], int] = defaultdict(int)
    team_last_played: dict[str, datetime.datetime] = {}
    h2h_history: dict[tuple[str, str], list[int]] = defaultdict(list)

    rows = []
    for m in matches:
        date = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        home, away = m["home"], m["away"]
        home_goals, away_goals = m["home_goals"], m["away_goals"]
        competition = m["competition"]
        season_label = f"{competition}-{date.year if date.month >= 7 else date.year - 1}"

        home_hist = team_history[home]
        away_hist = team_history[away]

        home_games_played = len(home_hist)
        away_games_played = len(away_hist)

        if home_games_played >= MIN_PRIOR_GAMES and away_games_played >= MIN_PRIOR_GAMES:
            home_rest = (date - team_last_played[home]).days if home in team_last_played else None
            away_rest = (date - team_last_played[away]).days if away in team_last_played else None

            pair_key = tuple(sorted([home, away]))
            h2h_goals = h2h_history[pair_key]

            rows.append({
                "date": date.date().isoformat(),
                "competition": competition,
                "matchday": m["matchday"],
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
                "home_rest_days": home_rest,
                "away_rest_days": away_rest,
                "h2h_games": len(h2h_goals),
                "h2h_avg_goals": (sum(h2h_goals) / len(h2h_goals)) if h2h_goals else None,
                "total_goals": home_goals + away_goals,
                "over_2_5": int((home_goals + away_goals) > 2.5),
            })

        # Now fold this match's actual result into both teams' history,
        # AFTER computing features, so nothing here leaks into itself.
        home_hist.append({"gf": home_goals, "ga": away_goals, "season_label": season_label})
        away_hist.append({"gf": away_goals, "ga": home_goals, "season_label": season_label})
        team_competition_games[(home, competition)] += 1
        team_competition_games[(away, competition)] += 1
        team_last_played[home] = date
        team_last_played[away] = date
        h2h_history[tuple(sorted([home, away]))].append(home_goals + away_goals)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows (dropped {len(matches) - len(rows)} early-season/insufficient-history games) to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
