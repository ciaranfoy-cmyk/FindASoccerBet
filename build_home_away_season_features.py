#!/usr/bin/env python3
"""Venue-split, and winsorized venue-split, versions of the season
goals-for/against/goal-diff features -- direct response to two concerns
raised about check_early_season_reliability.py's finding (early-season
picks are overconfident by +6.1pp): (1) the live home_gf_season/
away_gf_season/home_goal_diff/away_goal_diff features blend a team's
home AND away games from this season into one number (confirmed in
build_dataset_apifootball.py's season_avg()/table_standing() -- same
collinearity trap the venue-shots split (build_shots_venue_features.py)
and xG venue split already found and fixed for other stats), so a home
team's number is diluted by its away form and vice versa; (2) a small
early-season sample is one bad result away from being dominated by it
(a single 5-0 loss) -- the 95th percentile of a team's single-match
goal count in the full historical dataset is 4, so that's used as the
winsorization cap here, not a guessed number.

Both raw venue-split and winsorized venue-split variants are computed
so check_home_away_season_full_dataset.py can test swap-vs-combine and
raw-vs-winsorized against the live blended baseline the same way every
other feature in this project was gated: a rolling walk-forward
comparison is suggestive, but the full-dataset LogisticRegressionCV
coefficient survival check is the one this project actually trusts
(same test that overturned Elo and league-average-finish after they
looked like rolling-validation wins).

Usage:
    python3 build_home_away_season_features.py
"""

from collections import defaultdict

from build_dataset_apifootball import fetch_all_fixtures

WINSOR_CAP = 4  # 95th percentile of a team's single-match goal count, full dataset


def safe_div(total, n):
    return total / n if n else None


def main() -> None:
    print("Fetching full fixture history (cached)...")
    matches = fetch_all_fixtures(None)
    matches.sort(key=lambda m: m["date"])
    print(f"Replaying {len(matches)} matches chronologically...")

    # (team, competition, season) -> list of (gf, ga) for HOME games only / AWAY games only
    home_games: dict[tuple, list] = defaultdict(list)
    away_games: dict[tuple, list] = defaultdict(list)

    rows = []
    for i, m in enumerate(matches, start=1):
        home, away = m["home"], m["away"]
        competition, season = m["competition"], m["season"]
        hkey = (home, competition, season)
        akey = (away, competition, season)

        h_prior = home_games[hkey]
        a_prior = away_games[akey]

        def stats(prior: list) -> tuple:
            n = len(prior)
            if n == 0:
                return None, None, None, None
            raw_gf = safe_div(sum(g for g, _ in prior), n)
            raw_ga = safe_div(sum(a for _, a in prior), n)
            w_gf = safe_div(sum(min(g, WINSOR_CAP) for g, _ in prior), n)
            w_ga = safe_div(sum(min(a, WINSOR_CAP) for _, a in prior), n)
            return raw_gf, raw_ga, w_gf, w_ga

        home_gf_home, home_ga_home, home_gf_home_w, home_ga_home_w = stats(h_prior)
        away_gf_away, away_ga_away, away_gf_away_w, away_ga_away_w = stats(a_prior)

        home_gd_home = None if home_gf_home is None else home_gf_home - home_ga_home
        away_gd_away = None if away_gf_away is None else away_gf_away - away_ga_away
        home_gd_home_w = None if home_gf_home_w is None else home_gf_home_w - home_ga_home_w
        away_gd_away_w = None if away_gf_away_w is None else away_gf_away_w - away_ga_away_w

        rows.append({
            "fixture_id": m["fixture_id"],
            "home_gf_season_home": home_gf_home,
            "home_ga_season_home": home_ga_home,
            "away_gf_season_away": away_gf_away,
            "away_ga_season_away": away_ga_away,
            "home_goal_diff_home": home_gd_home,
            "away_goal_diff_away": away_gd_away,
            "goal_diff_gap_venue": None if home_gd_home is None or away_gd_away is None else abs(home_gd_home - away_gd_away),
            "home_games_this_venue_season": len(h_prior),
            "away_games_this_venue_season": len(a_prior),
            "home_gf_season_home_w95": home_gf_home_w,
            "home_ga_season_home_w95": home_ga_home_w,
            "away_gf_season_away_w95": away_gf_away_w,
            "away_ga_season_away_w95": away_ga_away_w,
            "home_goal_diff_home_w95": home_gd_home_w,
            "away_goal_diff_away_w95": away_gd_away_w,
            "goal_diff_gap_venue_w95": None if home_gd_home_w is None or away_gd_away_w is None else abs(home_gd_home_w - away_gd_away_w),
        })

        h_prior.append((m["home_goals"], m["away_goals"]))
        a_prior.append((m["away_goals"], m["home_goals"]))

        if i % 5000 == 0:
            print(f"  ...{i}/{len(matches)}")

    import pandas as pd
    out = pd.DataFrame(rows)
    out.to_csv("data/home_away_season_features.csv", index=False)
    print(f"Wrote {len(out)} rows to data/home_away_season_features.csv")


if __name__ == "__main__":
    main()
