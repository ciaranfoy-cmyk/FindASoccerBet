#!/usr/bin/env python3
"""Weekly Premier League matchweek analysis.

For a given matchweek, ranks fixtures by how (un)likely they are to finish
0-0, using a Poisson expected-goals model built from each team's home/away
scoring and conceding record over the last few completed seasons. Also
flags fixtures where a historically prolific home-scoring side hosts a
team that's both bottom-of-the-table and leaky defensively over that same
window.

Usage:
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py --matchday 5
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py --seasons 2022 2023 2024
"""

import argparse
import sys

import analysis
import football_data


def print_table(title: str, rows: list[tuple[str, object]], value_label: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for rank, (name, value) in enumerate(rows, start=1):
        print(f"{rank:>2}. {name:<30s} {value_label}={value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--competition", default="PL", help="Competition code, default PL")
    parser.add_argument("--matchday", type=int, default=None, help="Matchday to analyze; defaults to the next unplayed one")
    parser.add_argument("--seasons", type=int, nargs="+", default=None, help="Season start-years to use, e.g. 2022 2023 2024; defaults to the last 3 completed seasons")
    parser.add_argument("--top-n", type=int, default=5, help="How many top home-scoring teams to consider, default 5")
    parser.add_argument("--bottom-n", type=int, default=10, help="Size of the 'bottom of the table' / 'leaky defense' pools, default 10")
    args = parser.parse_args()

    try:
        seasons = args.seasons or football_data.recent_completed_seasons(n=3, competition=args.competition)
        matchday = args.matchday or football_data.next_matchday(competition=args.competition)

        print(f"Competition: {args.competition}   Matchday: {matchday}   Seasons used: {seasons}")

        standings_by_season = analysis.load_standings(seasons, competition=args.competition)
        team_stats = analysis.build_team_stats(standings_by_season)
        averages = analysis.league_averages(team_stats)

        top_scorers = analysis.top_home_scorers(team_stats, n=args.top_n)
        bottom_position = analysis.bottom_by_position(team_stats, n=args.bottom_n)
        bottom_defense = analysis.bottom_by_goals_conceded(team_stats, n=args.bottom_n)

        print_table(f"Top {args.top_n} home scorers", top_scorers, "home_goals")
        print_table(f"Bottom {args.bottom_n} by average finish", bottom_position, "avg_finish")
        print_table(f"Bottom {args.bottom_n} by goals conceded", bottom_defense, "goals_against")

        top_scorer_names = {name for name, _ in top_scorers}
        bottom_position_names = {name for name, _ in bottom_position}
        bottom_defense_names = {name for name, _ in bottom_defense}

        fixtures = football_data.get(
            f"/competitions/{args.competition}/matches", {"matchday": matchday}, ttl_seconds=0
        )["matches"]

    except football_data.FootballDataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    results = []
    for match in fixtures:
        home = match["homeTeam"]["name"]
        away = match["awayTeam"]["name"]
        eg = analysis.expected_goals(home, away, team_stats, averages)
        heuristic_hit = (
            home in top_scorer_names and away in bottom_position_names and away in bottom_defense_names
        )
        results.append({"match": match, "home": home, "away": away, "expected_goals": eg, "heuristic_hit": heuristic_hit})

    with_data = [r for r in results if r["expected_goals"] is not None]
    without_data = [r for r in results if r["expected_goals"] is None]

    for r in with_data:
        r["probs"] = analysis.match_probabilities(*r["expected_goals"])

    with_data.sort(key=lambda r: r["probs"]["p_0_0"])

    print(f"\nMatchday {matchday} fixtures — ranked least to most likely to finish 0-0")
    print("-" * 70)
    for r in with_data:
        p = r["probs"]
        flag = "  <-- fits home-scorer / bottom-table-leaky-defense heuristic" if r["heuristic_hit"] else ""
        print(
            f"{r['home']:<26s} vs {r['away']:<26s}  "
            f"xG {p['expected_goals_home']:.2f}-{p['expected_goals_away']:.2f}  "
            f"P(0-0)={p['p_0_0']*100:5.1f}%  P(BTTS)={p['p_btts']*100:5.1f}%  P(o2.5)={p['p_over_2_5']*100:5.1f}%{flag}"
        )

    if without_data:
        print("\nNo historical data for (likely newly promoted):")
        for r in without_data:
            print(f"  {r['home']} vs {r['away']}")

    heuristic_matches = [r for r in with_data if r["heuristic_hit"]]
    print("\nRecommendation (least likely 0-0):")
    if heuristic_matches:
        pick = heuristic_matches[0]
    elif with_data:
        pick = with_data[0]
    else:
        print("  Not enough data this matchday.")
        return 0

    p = pick["probs"]
    print(
        f"  {pick['home']} vs {pick['away']} — P(0-0) = {p['p_0_0']*100:.1f}%, "
        f"expected goals {p['expected_goals_home']:.2f}-{p['expected_goals_away']:.2f}"
    )
    print("  (Statistical read on scoring history only — not betting advice.)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
