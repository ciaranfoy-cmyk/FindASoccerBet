#!/usr/bin/env python3
"""Average league finishing position -- an alternative team-strength
measure to Elo (build_elo_features.py), proposed directly in response
to a real flaw Elo has: it pools ratings across divisions, so a team
promoted from the Championship carries its Championship-strength
rating straight into the Premier League with no penalty for the huge
step up in competition quality. Coventry hasn't played top-flight
football in 25 years, but Elo just imported their recent Championship
form unchanged.

This feature is explicitly division-aware instead: for every season
from THIS COMPETITION's first tracked season up to (not including) the
match's own season, look up the team's ACTUAL final rank in THIS
SPECIFIC COMPETITION (not their rank in whatever division they were
actually playing in that year). A season they weren't in this
competition at all counts as NOT_IN_LEAGUE_RANK (30) -- a team that's
spent the last several years in the Championship gets that many
straight seasons of "would-be 30th in the Prem" rather than their
Championship finishes bleeding through. Real, official final standings
(via /standings), not season-end table position reconstructed from
results.

Originally a fixed 7-season trailing window, which turned out to be
the reason this feature got zeroed out entirely when the live model
trained on the FULL dataset (it tested as a real improvement in
rolling_validation_league_finish.py's walk-forward folds first,
before that was diagnosed): a fixed window means the number of
seasons actually averaged silently varies row to row -- as few as 1
season for an early match in a league's own tracked history, capped
at exactly 7 for anything later -- so the same feature value carries a
different amount of real evidence depending on how far into the
dataset the match falls, diluting its signal across 11+ years of
pooled training data. An expanding window anchored at the
competition's own first season is honest about this instead: every
row's average reflects exactly however much real history exists by
that point, growing consistently rather than being arbitrarily capped.

Same no-lookahead discipline as everything else: a match in season S
only ever looks at seasons strictly before S.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_league_finish_features.py
"""

import csv
import datetime
import os
import sys

import apifootball
from build_dataset_apifootball import LEAGUES, fetch_all_fixtures

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "league_finish_features.csv")
NOT_IN_LEAGUE_RANK = 30


def fetch_standings(league_id: int, season: int) -> dict[str, int]:
    try:
        data = apifootball.get("/standings", {"league": league_id, "season": season})
    except apifootball.ApiFootballError:
        return {}
    response = data.get("response", [])
    if not response:
        return {}
    try:
        standings = response[0]["league"]["standings"][0]
    except (KeyError, IndexError):
        return {}
    return {row["team"]["name"]: row["rank"] for row in standings}


def build_standings_cache() -> dict[str, dict[int, dict[str, int]]]:
    """Shared by the historical build below and predict_upcoming.py's
    live scoring, so a live prediction uses the exact same lookup as
    training -- no risk of the two drifting apart.
    """
    current_year = datetime.date.today().year if datetime.date.today().month >= 7 else datetime.date.today().year - 1
    last_completed_season = current_year - 1

    standings_cache: dict[str, dict[int, dict[str, int]]] = {}
    for code, info in LEAGUES.items():
        standings_cache[code] = {}
        for season in range(info["first_season"], last_completed_season + 1):
            standings_cache[code][season] = fetch_standings(info["id"], season)
    return standings_cache


def avg_finish(
    team: str, competition: str, season: int, standings_cache: dict[str, dict[int, dict[str, int]]]
) -> float | None:
    first_season = LEAGUES[competition]["first_season"]
    window_seasons = range(first_season, season)
    if not window_seasons:
        return None
    ranks = [standings_cache.get(competition, {}).get(s, {}).get(team, NOT_IN_LEAGUE_RANK) for s in window_seasons]
    return sum(ranks) / len(ranks)


def add_league_finish_features(
    df: "pd.DataFrame", standings_cache: dict[str, dict[int, dict[str, int]]]
) -> "pd.DataFrame":
    """Shared by historical training data and predict_upcoming.py's live
    scorer -- both just need (team, competition, season) columns already
    present, so unlike venue-shots/xG there's no separate historical-CSV
    merge path needed: this computes the same way for a played match or
    an upcoming one. standings_cache is cheap to rebuild per run since
    apifootball.get() caches completed-season standings forever.
    """
    df["home_avg_finish"] = df.apply(
        lambda r: avg_finish(r["home_team"], r["competition"], r["season"], standings_cache), axis=1)
    df["away_avg_finish"] = df.apply(
        lambda r: avg_finish(r["away_team"], r["competition"], r["season"], standings_cache), axis=1)
    df["avg_finish_gap"] = df["away_avg_finish"] - df["home_avg_finish"]
    df["avg_finish_combined"] = df["home_avg_finish"] + df["away_avg_finish"]
    return df


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Fetching official final standings for every (competition, season)...")
    standings_cache = build_standings_cache()
    for code, seasons in standings_cache.items():
        for season, table in seasons.items():
            print(f"  {code} {season}: {len(table)} teams")

    print(f"\nProcessing {len(matches)} finished matches...")
    rows = []
    for i, m in enumerate(matches, start=1):
        home_finish = avg_finish(m["home"], m["competition"], m["season"], standings_cache)
        away_finish = avg_finish(m["away"], m["competition"], m["season"], standings_cache)
        rows.append({
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": m["competition"], "season": m["season"],
            "home_team": m["home"], "away_team": m["away"],
            "home_avg_finish": home_finish, "away_avg_finish": away_finish,
            "avg_finish_gap": (away_finish - home_finish) if (home_finish is not None and away_finish is not None) else None,
            "avg_finish_combined": (home_finish + away_finish) if (home_finish is not None and away_finish is not None) else None,
        })
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
