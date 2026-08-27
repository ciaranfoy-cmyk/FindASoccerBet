#!/usr/bin/env python3
"""Weekly Premier League matchweek analysis.

For a given matchweek, ranks fixtures by how (un)likely they are to finish
0-0, using a Poisson expected-goals model built from each team's home/away
scoring and conceding record over the last few completed seasons, blended
with each team's last few games of form (a multi-season baseline alone
can miss a real in-season trend — see --form-weight). Also flags fixtures
where a historically prolific home-scoring side hosts a team that's both
bottom-of-the-table and leaky defensively over that same window.

Also reports an over/under-2.5-goals read (ignoring home/away, since that
model was validated without it — see docs/over25-model-validation.md):
rather than forcing a fixed number of picks, it lists every fixture whose
predicted probability clears --over25-threshold, since backtesting a full
season showed the real edge only shows up above ~65% confidence and
appears in maybe 1-2 games a week, not every week.

Teams get their scoring/conceding record pulled from the Championship too
(when analyzing PL) so promoted/relegated sides keep some history instead
of being dropped entirely; a team with no top-flight games in the window
gets PROMOTION_PENALTY applied, since a Championship record alone tends to
overrate a promoted side (backtested from the 2025-26 promoted teams).

Usage:
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py --matchday 5
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py --seasons 2022 2023 2024
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py --no-form
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_matchweek.py --over25-threshold 0.65
"""

import argparse
import datetime
import sys

import analysis
import football_data

# PL <-> Championship is the only pairing this API's two English leagues
# support; used to give promoted/relegated teams cross-division history.
_SECONDARY_COMPETITION = {"PL": "ELC", "ELC": "PL"}


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
    parser.add_argument("--no-form", action="store_true", help="Skip the recent-form blend; use the season-history baseline only (faster, fewer API calls)")
    parser.add_argument("--form-games", type=int, default=5, help="Number of each team's most recent games to blend in, default 5")
    parser.add_argument("--form-weight", type=float, default=0.3, help="Weight given to recent form vs. season history, 0-1, default 0.3")
    parser.add_argument("--no-cross-division", action="store_true", help="Don't pull promoted/relegated teams' history from the other English league")
    parser.add_argument("--over25-threshold", type=float, default=0.60, help="Minimum P(over 2.5) to flag as a pick, default 0.60 (backtesting shows the real edge over typical odds starts closer to 0.65)")
    args = parser.parse_args()

    try:
        seasons = args.seasons or football_data.recent_completed_seasons(n=3, competition=args.competition)
        matchday = args.matchday or football_data.next_matchday(competition=args.competition)

        print(f"Competition: {args.competition}   Matchday: {matchday}   Seasons used: {seasons}")

        standings_by_season = analysis.load_standings(seasons, competition=args.competition)
        team_stats = analysis.build_team_stats(standings_by_season)
        averages = analysis.league_averages(team_stats)
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

    today = datetime.date.today().isoformat()
    use_form = not args.no_form
    if use_form:
        print(f"\nBlending in each team's last {args.form_games} games (weight={args.form_weight})... "
              f"this makes extra API calls and may take a couple of minutes under the free-tier rate limit.")

    results = []
    for match in fixtures:
        home = match["homeTeam"]["name"]
        away = match["awayTeam"]["name"]
        baseline_eg = analysis.expected_goals(home, away, combined_venue_stats, averages, top_flight=top_flight)

        eg = baseline_eg
        if use_form and baseline_eg is not None:
            eg = analysis.expected_goals_with_form(
                home, away,
                match["homeTeam"]["id"], match["awayTeam"]["id"],
                combined_venue_stats, averages,
                match["competition"]["id"], today,
                form_games=args.form_games, form_weight=args.form_weight,
                top_flight=top_flight,
            )

        heuristic_hit = (
            home in top_scorer_names and away in bottom_position_names and away in bottom_defense_names
        )
        novenue_eg = analysis.expected_goals_novenue(home, away, combined_overall_stats, pl_avg_goals, top_flight=top_flight)
        results.append({
            "match": match, "home": home, "away": away,
            "expected_goals": eg, "baseline_expected_goals": baseline_eg,
            "novenue_expected_goals": novenue_eg,
            "heuristic_hit": heuristic_hit,
        })

    with_data = [r for r in results if r["expected_goals"] is not None]
    without_data = [r for r in results if r["expected_goals"] is None]

    for r in with_data:
        r["probs"] = analysis.match_probabilities(*r["expected_goals"])
        r["baseline_probs"] = analysis.match_probabilities(*r["baseline_expected_goals"])

    with_data.sort(key=lambda r: r["probs"]["p_0_0"])

    form_note = " (form-blended)" if use_form else " (season history only)"
    print(f"\nMatchday {matchday} fixtures{form_note} — ranked least to most likely to finish 0-0")
    print("-" * 70)
    for r in with_data:
        p = r["probs"]
        flag = "  <-- fits home-scorer / bottom-table-leaky-defense heuristic" if r["heuristic_hit"] else ""
        baseline_note = ""
        if use_form:
            bp = r["baseline_probs"]
            baseline_note = f"  [season-only P(0-0)={bp['p_0_0']*100:4.1f}%]"
        print(
            f"{r['home']:<26s} vs {r['away']:<26s}  "
            f"xG {p['expected_goals_home']:.2f}-{p['expected_goals_away']:.2f}  "
            f"P(0-0)={p['p_0_0']*100:5.1f}%  P(BTTS)={p['p_btts']*100:5.1f}%  P(o2.5)={p['p_over_2_5']*100:5.1f}%{baseline_note}{flag}"
        )

    if without_data:
        print("\nNo historical data for (not in PL or Championship in the window used):")
        for r in without_data:
            print(f"  {r['home']} vs {r['away']}")

    novenue_with_data = [r for r in results if r["novenue_expected_goals"] is not None]
    for r in novenue_with_data:
        r["novenue_probs"] = analysis.match_probabilities(*r["novenue_expected_goals"])
    novenue_with_data.sort(key=lambda r: -r["novenue_probs"]["p_over_2_5"])

    print(f"\nOver 2.5 goals — non-venue attack/defense model, picks above {args.over25_threshold*100:.0f}% "
          f"(backtested across all of 2025-26: this model hit 60.5% on a forced top-5/week, "
          f"but only clears typical 1.60-odds breakeven — 62.5% — from ~65% confidence up, "
          f"which shows up in about 1-2 games a week, not every week)")
    print("-" * 70)
    any_pick = False
    for r in novenue_with_data:
        p = r["novenue_probs"]["p_over_2_5"]
        if p >= args.over25_threshold:
            any_pick = True
            print(f"  PICK  {r['home']:<26s} vs {r['away']:<26s}  P(o2.5)={p*100:5.1f}%  "
                  f"xG_total={r['novenue_probs']['expected_goals_total']:.2f}")
    if not any_pick:
        print("  No fixture this week clears the threshold — that's the point of the threshold, not a bug.")

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
    basis = "season history + recent form" if use_form else "season history only"
    print(f"  (Statistical read on {basis} — not betting advice.)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
