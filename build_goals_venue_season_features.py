#!/usr/bin/env python3
"""Season-level venue-specific goals/over-rate -- a more stable version of
build_goals_venue_features.py's 5-game rolling window, built after that
version failed the full-dataset check despite a real, checked pattern
prompting it: Man City's home games went over 2.5 in 14/19 (74%) in BOTH
2024-25 and 2025-26 independently. A 5-game rolling window can only ever
read 0/20/40/60/80/100% -- too coarse and noisy across ~15,000 matches
and dozens of teams to survive L1 regularization, even where a real
effect exists for one team. Two separate full seasons landing on the
exact same rate is a much larger, more stable sample than any 5-game
window can offer.

This feature instead computes, per (team, competition, venue), each
season's own over-rate, goals-for and goals-against average -- then
AVERAGES those season-level rates across every prior season, expanding
from the competition's own first tracked season. Same no-lookahead
discipline as build_league_finish_features.py: a match in season S only
ever looks at seasons strictly before S, and (unlike league-finish's
NOT_IN_LEAGUE_RANK penalty) a season the team didn't play at that venue
in this competition simply doesn't contribute a data point -- there's no
principled penalty value for an over-rate the way there is for a table
rank, so it's a straight average over qualifying seasons, not a padded
one.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_goals_venue_season_features.py
"""

import csv
import os
import sys
from collections import defaultdict

import apifootball
from build_dataset_apifootball import LEAGUES, fetch_all_fixtures

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "goals_venue_season_features.csv")
MIN_SEASON_GAMES = 3  # a season with fewer games at this venue doesn't count as a data point


def season_rate(
    team: str, competition: str, venue: str, season: int,
    season_agg: dict[tuple, dict[int, dict]],
) -> tuple[float | None, float | None, float | None, float | None]:
    """Average, across each season strictly before `season`, of that
    season's own (gf_avg, ga_avg, total_avg, over_pct) for this team at
    this venue in this competition. Seasons with < MIN_SEASON_GAMES
    games at that venue are skipped entirely, not penalized."""
    first_season = LEAGUES[competition]["first_season"]
    key = (team, competition, venue)
    seasons_data = season_agg.get(key, {})

    gf_rates, ga_rates, total_rates, over_rates = [], [], [], []
    for s in range(first_season, season):
        agg = seasons_data.get(s)
        if agg is None or agg["games"] < MIN_SEASON_GAMES:
            continue
        n = agg["games"]
        gf_rates.append(agg["gf"] / n)
        ga_rates.append(agg["ga"] / n)
        total_rates.append(agg["total"] / n)
        over_rates.append(agg["overs"] / n)

    if not gf_rates:
        return None, None, None, None
    return (
        sum(gf_rates) / len(gf_rates),
        sum(ga_rates) / len(ga_rates),
        sum(total_rates) / len(total_rates),
        sum(over_rates) / len(over_rates),
    )


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches (pure replay, no new API calls)")

    # (team, competition, venue) -> {season: {"games", "gf", "ga", "total", "overs"}}
    season_agg: dict[tuple, dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: {"games": 0, "gf": 0, "ga": 0, "total": 0, "overs": 0}))

    rows = []
    for i, m in enumerate(matches, start=1):
        home, away = m["home"], m["away"]
        competition, season = m["competition"], m["season"]

        home_gf, home_ga, home_total, home_over = season_rate(home, competition, "home", season, season_agg)
        away_gf, away_ga, away_total, away_over = season_rate(away, competition, "away", season, season_agg)

        rows.append({
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": competition, "season": season,
            "home_team": home, "away_team": away,
            "home_venue_gf_season_avg": home_gf, "home_venue_ga_season_avg": home_ga,
            "home_venue_total_season_avg": home_total, "home_venue_over_pct_season_avg": home_over,
            "away_venue_gf_season_avg": away_gf, "away_venue_ga_season_avg": away_ga,
            "away_venue_total_season_avg": away_total, "away_venue_over_pct_season_avg": away_over,
        })

        # Update state AFTER computing this match's pre-match features.
        home_goals, away_goals = m["home_goals"], m["away_goals"]
        total = home_goals + away_goals
        is_over = total > 2.5

        home_agg = season_agg[(home, competition, "home")][season]
        home_agg["games"] += 1
        home_agg["gf"] += home_goals
        home_agg["ga"] += away_goals
        home_agg["total"] += total
        home_agg["overs"] += int(is_over)

        away_agg = season_agg[(away, competition, "away")][season]
        away_agg["games"] += 1
        away_agg["gf"] += away_goals
        away_agg["ga"] += home_goals
        away_agg["total"] += total
        away_agg["overs"] += int(is_over)

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
