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
    """Aggregate each team's home/away goals-for, goals-against, and games played
    across all given seasons, plus average finishing position.

    Returns {team_name: {"home": {"gf", "ga", "played"}, "away": {...}, "positions": [...]}}
    """
    stats: dict[str, dict] = defaultdict(
        lambda: {
            "home": {"gf": 0, "ga": 0, "played": 0},
            "away": {"gf": 0, "ga": 0, "played": 0},
            "positions": [],
        }
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
        for row in tables["TOTAL"]:
            stats[row["team"]["name"]]["positions"].append(row["position"])

    return dict(stats)


def league_averages(team_stats: dict[str, dict]) -> dict[str, float]:
    home_gf = sum(s["home"]["gf"] for s in team_stats.values())
    home_played = sum(s["home"]["played"] for s in team_stats.values())
    away_gf = sum(s["away"]["gf"] for s in team_stats.values())
    away_played = sum(s["away"]["played"] for s in team_stats.values())
    return {
        "home_gf_per_game": home_gf / home_played,
        "away_gf_per_game": away_gf / away_played,
    }


def top_home_scorers(team_stats: dict[str, dict], n: int = 5) -> list[tuple[str, int]]:
    totals = [(name, s["home"]["gf"]) for name, s in team_stats.items() if s["home"]["played"]]
    return sorted(totals, key=lambda x: -x[1])[:n]


def bottom_by_position(team_stats: dict[str, dict], n: int = 10) -> list[tuple[str, float]]:
    averages = [
        (name, sum(s["positions"]) / len(s["positions"]))
        for name, s in team_stats.items()
        if s["positions"]
    ]
    return sorted(averages, key=lambda x: -x[1])[:n]


def bottom_by_goals_conceded(team_stats: dict[str, dict], n: int = 10) -> list[tuple[str, int]]:
    totals = [
        (name, s["home"]["ga"] + s["away"]["ga"])
        for name, s in team_stats.items()
        if s["home"]["played"] or s["away"]["played"]
    ]
    return sorted(totals, key=lambda x: -x[1])[:n]


def expected_goals(
    home_team: str, away_team: str, team_stats: dict[str, dict], averages: dict[str, float]
) -> tuple[float, float] | None:
    """Poisson expected goals for a fixture, from attack/defense strength ratios.

    Returns None if either team has no historical data in the given window
    (e.g. a newly promoted side).
    """
    home = team_stats.get(home_team)
    away = team_stats.get(away_team)
    if not home or not away or not home["home"]["played"] or not away["away"]["played"]:
        return None

    home_gf_pg = home["home"]["gf"] / home["home"]["played"]
    home_ga_pg = home["home"]["ga"] / home["home"]["played"]
    away_gf_pg = away["away"]["gf"] / away["away"]["played"]
    away_ga_pg = away["away"]["ga"] / away["away"]["played"]

    lambda_home = home_gf_pg * away_ga_pg / averages["home_gf_per_game"]
    lambda_away = away_gf_pg * home_ga_pg / averages["away_gf_per_game"]
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
