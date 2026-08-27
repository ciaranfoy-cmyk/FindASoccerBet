"""Historical scoring/conceding analysis over a window of completed PL seasons."""

from collections import defaultdict
from math import exp, factorial

import football_data


def load_standings(seasons: list[int], competition: str = "PL") -> dict[int, dict]:
    """Fetch (and cache) full standings for each season."""
    return {
        season: football_data.get(
            f"/competitions/{competition}/standings", {"season": season}, ttl_seconds=None
        )
        for season in seasons
    }


def _tables_by_type(standings: dict) -> dict[str, list[dict]]:
    return {t["type"]: t["table"] for t in standings["standings"]}


def build_team_stats(standings_by_season: dict[int, dict]) -> dict[str, dict]:
    """Aggregate each team's home/away goals-for, goals-against, and games
    played across all given seasons.

    Returns {team_name: {"home": {"gf", "ga", "played"}, "away": {...}}}
    """
    stats: dict[str, dict] = defaultdict(
        lambda: {"home": {"gf": 0, "ga": 0, "played": 0}, "away": {"gf": 0, "ga": 0, "played": 0}}
    )

    for standings in standings_by_season.values():
        tables = _tables_by_type(standings)
        for row in tables["HOME"]:
            side = stats[row["team"]["name"]]["home"]
            side["gf"] += row["goalsFor"]
            side["ga"] += row["goalsAgainst"]
            side["played"] += row["playedGames"]
        for row in tables["AWAY"]:
            side = stats[row["team"]["name"]]["away"]
            side["gf"] += row["goalsFor"]
            side["ga"] += row["goalsAgainst"]
            side["played"] += row["playedGames"]

    return dict(stats)


def build_combined_team_stats(*standings_by_season_sets: dict[int, dict]) -> dict[str, dict]:
    """Merge home/away goal stats across multiple competitions' standings
    (e.g. Premier League + Championship), so a team keeps its scoring/
    conceding history through promotion or relegation instead of losing it
    entirely when it changes divisions.

    Positions aren't included here — league position isn't comparable
    across divisions, so use build_team_stats (single competition) for
    position-based rankings.
    """
    stats: dict[str, dict] = defaultdict(
        lambda: {"home": {"gf": 0, "ga": 0, "played": 0}, "away": {"gf": 0, "ga": 0, "played": 0}}
    )
    for standings_by_season in standings_by_season_sets:
        for standings in standings_by_season.values():
            tables = _tables_by_type(standings)
            for row in tables["HOME"]:
                side = stats[row["team"]["name"]]["home"]
                side["gf"] += row["goalsFor"]
                side["ga"] += row["goalsAgainst"]
                side["played"] += row["playedGames"]
            for row in tables["AWAY"]:
                side = stats[row["team"]["name"]]["away"]
                side["gf"] += row["goalsFor"]
                side["ga"] += row["goalsAgainst"]
                side["played"] += row["playedGames"]
    return dict(stats)


def top_flight_teams(primary_team_stats: dict[str, dict]) -> set[str]:
    """Names of teams that have played top-flight games in the window used
    to build primary_team_stats (i.e. have real PL history, not just a
    Championship record merged in via build_combined_team_stats).
    """
    return {
        name
        for name, s in primary_team_stats.items()
        if s["home"]["played"] or s["away"]["played"]
    }


# Applied to a team's per-game rates when it has no top-flight history in
# the analysis window (i.e. its stats come entirely from a lower division).
# Derived empirically from the three teams promoted into the Premier League
# for 2025-26 (Burnley, Leeds United, Sunderland): on average they scored
# 0.34 fewer goals/game and conceded 0.58 more goals/game than their
# Championship-blended baseline predicted — the step up in competition
# hits a promoted side's defense harder than its attack. Small sample
# (n=3), but directionally consistent across all three, so worth applying;
# revisit as more promoted-team seasons of data become available.
PROMOTION_PENALTY = {"attack": -0.34, "defense": 0.58}


def league_average_goals(standings_by_season: dict[int, dict]) -> float:
    """Overall average goals scored per team per game across a single
    competition's standings — the normalizer expected_goals_novenue needs
    for that competition, separate from the merged cross-division dict
    (which shouldn't be used to compute an average, since it blends two
    different scoring environments together).
    """
    total_gf = total_played = 0
    for standings in standings_by_season.values():
        for row in _tables_by_type(standings)["TOTAL"]:
            total_gf += row["goalsFor"]
            total_played += row["playedGames"]
    return total_gf / total_played


def _apply_promotion_penalty(
    gf_pg: float, ga_pg: float, team: str, top_flight: set[str] | None
) -> tuple[float, float]:
    if top_flight is not None and team not in top_flight:
        gf_pg = max(0.0, gf_pg + PROMOTION_PENALTY["attack"])
        ga_pg = ga_pg + PROMOTION_PENALTY["defense"]
    return gf_pg, ga_pg


def expected_goals_novenue(
    home_team: str,
    away_team: str,
    team_stats: dict[str, dict],
    league_avg_goals_per_game: float,
    top_flight: set[str] | None = None,
) -> tuple[float, float] | None:
    """Poisson expected goals ignoring home/away splits — each team's
    overall (any-venue) goals-for/against rate vs. the other's, normalized
    by the competition's overall average goals/team/game.

    team_stats here should carry overall "gf"/"ga"/"played" per team (see
    build_combined_team_stats), not the home/away split used by
    expected_goals.
    """
    home = team_stats.get(home_team)
    away = team_stats.get(away_team)
    if not home or not away or not home["played"] or not away["played"]:
        return None

    home_gf_pg, home_ga_pg = home["gf"] / home["played"], home["ga"] / home["played"]
    away_gf_pg, away_ga_pg = away["gf"] / away["played"], away["ga"] / away["played"]

    home_gf_pg, home_ga_pg = _apply_promotion_penalty(home_gf_pg, home_ga_pg, home_team, top_flight)
    away_gf_pg, away_ga_pg = _apply_promotion_penalty(away_gf_pg, away_ga_pg, away_team, top_flight)

    avg = league_avg_goals_per_game
    lambda_home = avg * (home_gf_pg / avg) * (away_ga_pg / avg)
    lambda_away = avg * (away_gf_pg / avg) * (home_ga_pg / avg)
    return lambda_home, lambda_away


def _poisson_pmf(k: int, lam: float) -> float:
    return exp(-lam) * lam**k / factorial(k)


def match_probabilities(lambda_home: float, lambda_away: float, max_goals: int = 9) -> dict:
    p_home_scoreless = _poisson_pmf(0, lambda_home)
    p_away_scoreless = _poisson_pmf(0, lambda_away)
    p_0_0 = p_home_scoreless * p_away_scoreless
    p_btts = (1 - p_home_scoreless) * (1 - p_away_scoreless)

    p_two_or_fewer = sum(
        _poisson_pmf(h, lambda_home) * _poisson_pmf(a, lambda_away)
        for h in range(3)
        for a in range(3)
        if h + a <= 2
    )

    return {
        "expected_goals_home": lambda_home,
        "expected_goals_away": lambda_away,
        "expected_goals_total": lambda_home + lambda_away,
        "p_0_0": p_0_0,
        "p_btts": p_btts,
        "p_over_2_5": 1 - p_two_or_fewer,
        "p_home_scoreless": p_home_scoreless,
        "p_away_scoreless": p_away_scoreless,
    }
