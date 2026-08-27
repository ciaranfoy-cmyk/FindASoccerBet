#!/usr/bin/env python3
"""Weekly Premier League over/under-2.5-goals analysis.

For a given matchweek, flags fixtures whose predicted probability of
going over 2.5 goals clears --over25-threshold. Uses a non-venue Poisson
model — each team's overall goals-for/against rate vs. the opponent's,
ignoring home/away — since that's the version that was actually
backtested; see docs/over25-model-validation.md for the full numbers.

It deliberately does NOT force a fixed number of picks per week: a full
2025-26 season backtest showed the model's real edge over typical
bookmaker odds (~1.60, i.e. "win $60 on $100") only shows up from ~65%
confidence upward, in about 1-2 games a week — a forced weekly pick count
dilutes the very thing worth using this for. Some weeks will show no
picks at all; that's the threshold working as intended, not a bug.

Promoted/relegated teams get their scoring record pulled from the
Championship too (this API's only other English league), so they're not
simply excluded for lack of Premier League history. A team with no
top-flight games in the window gets a defensive/attacking penalty applied
on top, since a Championship record alone tends to overrate a promoted
side — see PROMOTION_PENALTY in analysis.py.

Usage:
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py --matchday 5
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py --seasons 2022 2023 2024
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py --over25-threshold 0.65
"""

import argparse
import sys

import analysis
import football_data

# PL <-> Championship is the only pairing this API's two English leagues
# support; used to give promoted/relegated teams cross-division history.
_SECONDARY_COMPETITION = {"PL": "ELC", "ELC": "PL"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--competition", default="PL", help="Competition code, default PL")
    parser.add_argument("--matchday", type=int, default=None, help="Matchday to analyze; defaults to the next unplayed one")
    parser.add_argument("--seasons", type=int, nargs="+", default=None, help="Season start-years to use, e.g. 2022 2023 2024; defaults to the last 3 completed seasons")
    parser.add_argument("--no-cross-division", action="store_true", help="Don't pull promoted/relegated teams' history from the other English league")
    parser.add_argument("--over25-threshold", type=float, default=0.60, help="Minimum P(over 2.5) to flag as a pick, default 0.60 (backtesting shows the real edge over typical odds starts closer to 0.65)")
    args = parser.parse_args()

    try:
        seasons = args.seasons or football_data.recent_completed_seasons(n=3, competition=args.competition)
        matchday = args.matchday or football_data.next_matchday(competition=args.competition)

        print(f"Competition: {args.competition}   Matchday: {matchday}   Seasons used: {seasons}")

        standings_by_season = analysis.load_standings(seasons, competition=args.competition)
        team_stats = analysis.build_team_stats(standings_by_season)
        pl_avg_goals = analysis.league_average_goals(standings_by_season)
        top_flight = analysis.top_flight_teams(team_stats)

        secondary_code = None if args.no_cross_division else _SECONDARY_COMPETITION.get(args.competition)
        if secondary_code:
            secondary_standings = analysis.load_standings(seasons, competition=secondary_code)
            combined_venue_stats = analysis.build_combined_team_stats(standings_by_season, secondary_standings)
        else:
            combined_venue_stats = team_stats
        combined_overall_stats = {
            name: {
                "gf": s["home"]["gf"] + s["away"]["gf"],
                "ga": s["home"]["ga"] + s["away"]["ga"],
                "played": s["home"]["played"] + s["away"]["played"],
            }
            for name, s in combined_venue_stats.items()
        }

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
        eg = analysis.expected_goals_novenue(home, away, combined_overall_stats, pl_avg_goals, top_flight=top_flight)
        results.append({"home": home, "away": away, "expected_goals": eg})

    with_data = [r for r in results if r["expected_goals"] is not None]
    without_data = [r for r in results if r["expected_goals"] is None]

    for r in with_data:
        r["probs"] = analysis.match_probabilities(*r["expected_goals"])
    with_data.sort(key=lambda r: -r["probs"]["p_over_2_5"])

    print(f"\nMatchday {matchday} — over 2.5 goals, ranked most to least likely")
    print("-" * 70)
    picks = 0
    for r in with_data:
        p = r["probs"]
        pick = p["p_over_2_5"] >= args.over25_threshold
        marker = "PICK  " if pick else "      "
        if pick:
            picks += 1
        print(
            f"{marker}{r['home']:<26s} vs {r['away']:<26s}  "
            f"xG_total={p['expected_goals_total']:.2f}  "
            f"P(o2.5)={p['p_over_2_5']*100:5.1f}%  P(BTTS)={p['p_btts']*100:5.1f}%"
        )

    if without_data:
        print("\nNo historical data for (not in PL or Championship in the window used):")
        for r in without_data:
            print(f"  {r['home']} vs {r['away']}")

    print(f"\n{picks} fixture(s) clear the {args.over25_threshold*100:.0f}% threshold this week.")
    if picks == 0:
        print("That's expected some weeks, not an error — see docs/over25-model-validation.md: "
              "the model's real edge over typical odds only shows up above ~65% confidence, "
              "in roughly 1-2 games a week on average, not every week.")
    print("Statistical read on scoring/conceding history — not betting advice.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
