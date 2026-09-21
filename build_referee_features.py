#!/usr/bin/env python3
"""Referee tendency features -- API-Football already returns the
referee's name on every /fixtures response (m["fixture"]["referee"]),
but nothing in this project has ever used it. Cards/fouls are already
in the /fixtures/statistics response this project fetches for shot
stats (build_dataset_apifootball.py's shot_stats_for), so this needs
ZERO new API calls beyond what's already cached on disk.

Hypothesis being tested (see build_referee_features_validation.py for
the actual walk-forward Brier check): a referee's own historical
tendency -- how many total goals/cards games they officiate tend to
have -- might carry signal about game flow/pace independent of the two
teams' own stats. Untested until the validation script says so; this
file only BUILDS the candidate features, it does not claim they help.

Same no-lookahead discipline as every other feature builder here: a
referee's rolling averages only include games officiated strictly
before the one being scored. Referee identity keys on the raw name
string API-Football returns (format varies by league -- "M. Oliver"
vs "Paulo Cesar Zanovelli da Silva, Brazil" -- but referee pools are
effectively disjoint per country/competition in this dataset, so exact
string match is safe; no cross-league collision risk worth handling).

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_referee_features.py
"""

import csv
import os
import sys
from collections import defaultdict, deque

import apifootball
from build_dataset_apifootball import fetch_all_fixtures

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "referee_features.csv")
ROLLING_N = 15
MIN_GAMES_FOR_ROLLING = 8


def card_and_foul_stats_for(fixture_id: int) -> dict:
    """Total (both teams combined) yellow+red cards and fouls for a match,
    re-parsed from the same cached /fixtures/statistics response
    build_xg_features.py already reads for real xG -- no new API call."""
    data = apifootball.get("/fixtures/statistics", {"fixture": fixture_id})
    total_cards = 0
    total_fouls = 0
    have_data = False
    for team_block in data.get("response", []):
        stats = {s["type"]: s["value"] for s in team_block["statistics"]}
        yellow = stats.get("Yellow Cards")
        red = stats.get("Red Cards")
        fouls = stats.get("Fouls")
        if yellow is not None:
            total_cards += int(yellow)
            have_data = True
        if red is not None:
            total_cards += int(red)
            have_data = True
        if fouls is not None:
            total_fouls += int(fouls)
            have_data = True
    return {"cards": total_cards, "fouls": total_fouls} if have_data else {}


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

    print(f"Processing {len(matches)} finished matches (re-parsing cached /fixtures + /fixtures/statistics)")

    ref_goals_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))
    ref_cards_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))
    ref_fouls_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLLING_N))
    ref_games_officiated: dict[str, int] = defaultdict(int)

    rows = []
    n_no_referee = 0
    for i, m in enumerate(matches, start=1):
        ref = m.get("referee")
        if not ref:
            n_no_referee += 1
            row = {
                "fixture_id": m["fixture_id"], "date": m["date"][:10],
                "competition": m["competition"], "season": m["season"],
                "referee": None,
                "ref_avg_goals_last15": None, "ref_avg_cards_last15": None,
                "ref_avg_fouls_last15": None, "ref_games_officiated": None,
            }
            rows.append(row)
            continue

        row = {
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": m["competition"], "season": m["season"],
            "referee": ref,
            "ref_avg_goals_last15": rolling_avg(ref_goals_history[ref]),
            "ref_avg_cards_last15": rolling_avg(ref_cards_history[ref]),
            "ref_avg_fouls_last15": rolling_avg(ref_fouls_history[ref]),
            "ref_games_officiated": ref_games_officiated[ref],
        }
        rows.append(row)

        try:
            stats = card_and_foul_stats_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            stats = {}

        total_goals = m["home_goals"] + m["away_goals"]
        ref_goals_history[ref].append(total_goals)
        if stats:
            ref_cards_history[ref].append(stats["cards"])
            ref_fouls_history[ref].append(stats["fouls"])
        ref_games_officiated[ref] += 1

        if i % 5000 == 0:
            print(f"  ...{i}/{len(matches)}")

    n_with_feature = sum(1 for r in rows if r["ref_avg_goals_last15"] is not None)
    print(f"{n_no_referee} matches with no referee assigned in API-Football")
    print(f"{n_with_feature}/{len(rows)} matches have a referee with >= {MIN_GAMES_FOR_ROLLING} prior officiated games")

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
