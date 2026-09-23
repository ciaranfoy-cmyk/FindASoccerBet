#!/usr/bin/env python3
"""What are the actual picks, and why? This is the single authoritative
answer -- not a raw data dump you have to reconcile against another
tool's output yourself.

A fixture only counts as a real pick if it clears ALL THREE bars:
  1. The model is genuinely confident -- its calibrated probability
     clears the rolling p95 bar (same live selection rule
     forward_test_log.py uses, computed fresh from the model's own
     trailing historical predictions, not picked after the fact).
  2. There's a real, currently-live price to compare against, on
     Kalshi and/or Polymarket -- a confident fixture with no market to
     trade it on is not a recommendation, just a number. Never shown
     as a pick.
  3. The model disagrees with that price in the profitable direction
     (positive edge) on AT LEAST ONE of the two books -- we only need
     one tradeable edge to have a real pick, not agreement from both.
     When both show edge, both are reported; when only one does, the
     pick is still real, just only tradeable on that book.

A big "edge" on a fixture the model itself only rates a coin flip is
NOT a real pick -- it just means Kalshi's price is even more extreme
than a so-so model number, which is a much weaker signal. Only
fixtures clearing all three bars are ever shown as picks; --no-bar
shows everything for exploration, clearly separated from the real list.

Separately, a WATCHLIST section always prints: any fixture where the
model is genuinely confident (>= WATCHLIST_MIN_P, a plain probability
floor, not the statistical p95 bar above) AND Kalshi is pricing it
within WATCHLIST_MAX_ABS_EDGE of that number -- regardless of edge
sign. This is NOT a pick list -- it's model/market agreement, worth
seeing on its own terms (two independent sources landing in the same
place is itself informative, even with zero exploitable edge).

Each pick gets a feature-contribution breakdown: predicted log-odds =
sum(coefficient_i * standardized_feature_i) + intercept, so every term
is real arithmetic the model actually did, not a post-hoc guess.

Usage:
    APIFOOTBALL_KEY=xxxx python3 explain_picks.py                  # the real picks, ranked by edge
    APIFOOTBALL_KEY=xxxx python3 explain_picks.py --days 14 --top 5
    APIFOOTBALL_KEY=xxxx python3 explain_picks.py --no-bar          # explore everything, no confidence filter
    APIFOOTBALL_KEY=xxxx python3 explain_picks.py --by-probability  # ignore Kalshi, rank confident picks by raw P
    APIFOOTBALL_KEY=xxxx python3 explain_picks.py --fixture-id 1557393
"""

import argparse
import os
import warnings

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import model_cache
from analyze_dataset_apifootball import add_derived_features
from analyze_player_form import add_player_form_derived_features
from analyze_shots_venue import (
    add_shots_venue_derived_features,
    load_with_player_form_and_shots_venue,
    load_with_xg_player_form_and_shots_venue,
)
from build_xg_weighted_features import (
    GEO_FEATURES,
    WEIGHTED_XG_RAW_FEATURES,
    add_geo_mean_features,
    add_weighted_xg_derived_features,
    load_weighted_xg,
)
from build_team_ratings_features import (
    RATINGS_DERIVED_FEATURES,
    RATINGS_RAW_FEATURES,
    add_ratings_derived_features,
    load_team_ratings,
)
from build_league_finish_features import add_league_finish_features, build_standings_cache
from calibration import apply_calibration, load_calibrators
from forward_test_log import (
    EARLY_SEASON_CUTOFF,
    KALSHI_SERIES_BY_COMPETITION,
    OVER_DISALLOWED_COMPETITIONS,
    UNDER_DISALLOWED_COMPETITIONS,
    _calibrated_stream,
    compute_pool_hit_rate,
    compute_rolling_p95_bar,
    compute_rolling_p95_under_bar,
    fetch_kalshi_over25_for_series,
    kalshi_fee,
    team_games_into_season_live,
)
from live_kalshi_edge_test import _normalize
from polymarket_prices import (
    POLYMARKET_TAG_BY_COMPETITION,
    _poly_names_match,
    fetch_polymarket_over25_for_league,
    polymarket_fee,
)
from predict_upcoming import (
    CORE_CANDIDATES,
    ODDS_CANDIDATES,
    TRAINING_DATA_CUTOFF,
    XG_CANDIDATES,
    XG_FINISHING_FEATURES,
    XG_ODDS_CANDIDATES,
    build_feature_row,
    fetch_upcoming_fixtures,
    replay_to_current_state,
)
from build_dataset_apifootball import fetch_all_fixtures
from market_odds_features import (
    COVERED_COMPETITIONS,
    MARKET_ODDS_ENABLED,
    StatsApiError,
    _devig_over,
    _find_match_id,
    _get,
    fetch_market_over25_prob,
)

warnings.filterwarnings("ignore")

# Watchlist thresholds -- separate from the p95 confidence bar and the
# is_pick gate. "North of 60% and Kalshi's within ~2pp of that" is worth
# seeing even with no edge: two independent sources (the model and a
# market priced by professional makers) landing in the same place.
WATCHLIST_MIN_P = 0.60
WATCHLIST_MAX_ABS_EDGE = 0.02

# Plain-English descriptions for the features that actually tend to
# survive L1 selection, keyed on the RAW feature name with {home}/{away}
# placeholders swapped in by _describe(). Anything not in this dict
# just prints its raw name -- better an ugly label than a wrong one.
DESCRIPTIONS = {
    "mkt_over25_prob": "Bet365's own devigged Over 2.5 probability (independent market, not Kalshi/Polymarket -- see market_odds_features.py)",
    "home_goal_diff": "{home}'s season goal difference",
    "away_goal_diff": "{away}'s season goal difference",
    "goal_diff_gap": "gap between the two teams' season goal difference",
    "home_league_position": "{home}'s league position",
    "away_league_position": "{away}'s league position",
    "position_gap": "gap in league position between the two teams",
    "home_points": "{home}'s season points",
    "away_points": "{away}'s season points",
    "is_PL": "this is a Premier League match (historically higher-scoring)",
    "h2h_avg_goals": "average goals in this exact head-to-head matchup",
    "h2h_avg_goals_shrunk": "average goals in this head-to-head matchup, discounted toward the league norm when there's little history behind it",
    "h2h_games": "number of tracked head-to-head meetings",
    "home_missing_players": "{home}'s missing/injured player count",
    "away_missing_players": "{away}'s missing/injured player count",
    "missing_players_total": "combined missing/injured players, both teams",
    "missing_players_gap": "gap in missing players between the two teams",
    "home_rest_days": "{home}'s days of rest since their last match",
    "away_rest_days": "{away}'s days of rest since their last match",
    "home_clean_sheet_pct_last5": "{home}'s clean-sheet rate, last 5 games",
    "away_clean_sheet_pct_last5": "{away}'s clean-sheet rate, last 5 games",
    "home_clean_sheet_pct_last10": "{home}'s clean-sheet rate, last 10 games",
    "away_clean_sheet_pct_last10": "{away}'s clean-sheet rate, last 10 games",
    "clean_sheet_pct_combined_last5": "combined clean-sheet rate, both teams, last 5",
    "home_ga_last5": "goals {home} has CONCEDED in their last 5 (leaky defense = more goals)",
    "away_ga_last5": "goals {away} has CONCEDED in their last 5",
    "home_ga_last10": "goals {home} has conceded in their last 10",
    "away_ga_last10": "goals {away} has conceded in their last 10",
    "home_ga_season": "goals {home} has conceded this season",
    "away_ga_season": "goals {away} has conceded this season",
    "combined_ga_last5": "combined goals conceded by both teams, last 5",
    "home_gf_last5": "goals {home} has scored in their last 5 (blended home+away)",
    "away_gf_last5": "goals {away} has scored in their last 5 (blended home+away)",
    "home_gf_last10": "goals {home} has scored in their last 10",
    "away_gf_last10": "goals {away} has scored in their last 10",
    "home_gf_season": "goals {home} has scored this season",
    "away_gf_season": "goals {away} has scored this season",
    "combined_gf_last5": "combined recent scoring form, both teams, last 5",
    "naive_expected_total_last5": "simple expected-goals estimate from recent scoring/conceding",
    "home_shots_last5": "{home}'s total shots, last 5 (blended)",
    "away_shots_last5": "{away}'s total shots, last 5 (blended)",
    "home_shots_on_goal_last5": "{home}'s shots ON TARGET, last 5",
    "away_shots_on_goal_last5": "{away}'s shots on target, last 5",
    "home_shots_inside_box_last5": "{home}'s shots from inside the box, last 5",
    "away_shots_inside_box_last5": "{away}'s shots from inside the box, last 5",
    "combined_shots_last5": "combined total shots, both teams, last 5",
    "combined_shots_inside_box_last5": "combined shots from inside the box, both teams -- a strong signal historically",
    "shots_gap_last5": "gap in shot volume between the two teams",
    "home_conversion_rate_last5": "{home}'s goals-per-shot rate, last 5 (very high = due for regression)",
    "away_conversion_rate_last5": "{away}'s goals-per-shot rate, last 5",
    "home_venue_shots_last5": "{home}'s shots SPECIFICALLY in their last 5 HOME games",
    "away_venue_shots_last5": "{away}'s shots SPECIFICALLY in their last 5 AWAY games",
    "home_venue_shots_on_goal_last5": "{home}'s shots on target specifically at home",
    "away_venue_shots_on_goal_last5": "{away}'s shots on target specifically away",
    "home_venue_shots_inside_box_last5": "{home}'s box shots specifically at home",
    "away_venue_shots_inside_box_last5": "{away}'s box shots specifically away",
    "combined_venue_shots_last5": "combined venue-specific shot volume -- both teams' home/away form",
    "combined_venue_shots_inside_box_last5": "combined venue-specific box shots -- the single strongest shot signal found this project",
    "venue_shots_gap_last5": "gap in venue-specific shot volume",
    "home_venue_conversion_rate_last5": "{home}'s goals-per-shot rate specifically at home",
    "away_venue_conversion_rate_last5": "{away}'s goals-per-shot rate specifically away",
    "home_attacking_form": "{home}'s STARTING attackers' individual scoring form (not just team goals)",
    "away_attacking_form": "{away}'s starting attackers' individual scoring form",
    "attacking_form_total": "combined individual attacking form of both teams' actual starters",
    "attacking_form_gap": "gap in individual attacking form between the two teams",
    "home_xg_last5": "{home}'s real expected-goals (xG), last 5 -- shot QUALITY, not just volume",
    "away_xg_last5": "{away}'s real xG, last 5",
    "home_xg_against_last5": "xG {home} has conceded, last 5 (defensive leakiness by shot quality)",
    "away_xg_against_last5": "xG {away} has conceded, last 5",
    "combined_xg_last5": "combined real xG, both teams -- historically the single strongest predictor",
    "xg_gap_last5": "gap in real xG between the two teams",
    "naive_expected_total_xg_last5": "sum of both teams' attack + opponent leakiness in real xG terms -- the model's single most important number when available",
    "poisson_p_over_last5": "true Poisson probability of 3+ goals given the xG rate (not just the raw mean)",
    "home_xg_last5_weighted": "{home}'s real xG, recency + competition weighted (recent games count more; games in a different competition count less)",
    "away_xg_last5_weighted": "{away}'s real xG, recency + competition weighted",
    "home_xg_against_last5_weighted": "xG {home} has conceded, recency + competition weighted",
    "away_xg_against_last5_weighted": "xG {away} has conceded, recency + competition weighted",
    "combined_xg_last5_weighted": "combined recency/competition-weighted xG, both teams",
    "xg_gap_last5_weighted": "gap in recency/competition-weighted xG between the two teams",
    "naive_expected_total_xg_last5_weighted": "sum of both teams' attack + opponent leakiness, recency/competition-weighted xG terms",
    "poisson_p_over_last5_weighted": "true Poisson probability of 3+ goals given the recency/competition-weighted xG rate -- the model's single strongest predictor, replacing the flat (unweighted) version",
    "home_finishing_last5": "{home}'s actual goals minus xG, last 5 (over/under-performing their chances)",
    "away_finishing_last5": "{away}'s actual goals minus xG, last 5",
    "season_year": "the calendar year itself -- the model has learned recent seasons trend higher-scoring than older ones",
    "min_competition_experience": "how established the less-tenured of the two teams is in this competition",
    "home_attack_rating": "{home}'s attack rating -- a jointly-solved, opponent-adjusted regression, not a flat average",
    "away_attack_rating": "{away}'s attack rating -- jointly-solved, opponent-adjusted",
    "home_defense_rating": "{home}'s defense rating (higher = leakier) -- jointly-solved, opponent-adjusted",
    "away_defense_rating": "{away}'s defense rating (higher = leakier) -- jointly-solved, opponent-adjusted",
    "home_expected_rating": "{home}'s expected goals from the two-way attack/defense rating system",
    "away_expected_rating": "{away}'s expected goals from the two-way attack/defense rating system",
    "combined_expected_rating": "combined expected goals from the two-way (attack/defense) team-quality ratings",
    "rating_gap": "gap between the two teams' expected goals under the two-way rating system",
    "home_expected_geo": "{home}'s expected goals -- geometric mean of their own attack and the opponent's defense",
    "away_expected_geo": "{away}'s expected goals -- geometric mean of their own attack and the opponent's defense",
    "combined_expected_geo": "combined expected goals, geometric-mean combiner -- discounts a leaky defense when the opponent's own attack hasn't been sharp enough to exploit it",
    "poisson_p_over_geo": "true Poisson probability of 3+ goals given the geometric-mean expected-goals rate",
}


def _describe(feat: str, home: str, away: str) -> str:
    template = DESCRIPTIONS.get(feat)
    if template is None:
        return feat
    return template.format(home=home, away=away)


def _fetch_bet365_fair(competition: str, home: str, away: str, date: str) -> float | None:
    """Bet365's de-vigged Over-2.5 fair value, for --table's reference
    column only -- bypasses MARKET_ODDS_ENABLED the same way
    weekend_overlap.py's retrospective fetch did, since this is a
    diagnostic display column (is Bet365 seeing something different
    from Kalshi/Polymarket), not a live model input. _find_match_id
    needs a plain date, not the full ISO timestamp fetch_upcoming_
    fixtures() returns -- callers must pass date[:10].
    """
    comp_id = COVERED_COMPETITIONS.get(competition)
    if comp_id is None:
        return None
    try:
        match_id = _find_match_id(comp_id, home, away, date)
        if match_id is None:
            return None
        odds = _get(f"/football/matches/{match_id}/odds")
        bk = next((b for b in odds["data"]["bookmakers"] if b["bookmaker"] == "Bet365"), None)
        if bk is None:
            return None
        tg = bk["markets"].get("total_goals", {})
        line = tg.get("2.5", {})
        return _devig_over(line.get("over", {}).get("last_seen"), line.get("under", {}).get("last_seen"))
    except StatsApiError:
        return None


TABLE_MIN_MODEL_P = 0.60


def _xg_odds_prob(r: pd.Series, bet365: float | None, xg_odds_model, xg_odds_scaler, calibrators: dict) -> float | None:
    """The xG+odds cohort's calibrated Over-2.5 probability for one live
    fixture, computed on demand regardless of MARKET_ODDS_ENABLED --
    this is a second, purely informational cohort for --table, not a
    live pick input. Needs real xG features AND a Bet365 price (the
    bypass fetch, since the kill switch means live_df's own
    mkt_over25_prob column is never populated) -- returns None if
    either is missing, same "fall back to None, never crash" pattern
    as _fetch_bet365_fair().
    """
    if xg_odds_model is None or bet365 is None or not r["has_xg"]:
        return None
    row = r[XG_CANDIDATES].to_frame().T.copy()
    row["mkt_over25_prob"] = bet365
    raw_p = xg_odds_model.predict_proba(xg_odds_scaler.transform(row[XG_ODDS_CANDIDATES]))[:, 1][0]
    # "xG+odds" reuses the "xG" calibrator -- same approximation
    # main()'s calibration_lookup uses for the live scoring path.
    calibrated = apply_calibration(pd.Series([raw_p]), pd.Series(["xG"]), calibrators)
    return float(calibrated.iloc[0])


def _print_table(rows: list[dict], value_cols: tuple[str, ...]) -> None:
    table = pd.DataFrame(rows)
    table["sort_key"] = table["edge_pp"].fillna(-999)
    table = table.sort_values("sort_key", ascending=False).drop(columns="sort_key")
    for col in value_cols:
        signed = col == "edge_pp"
        table[col] = table[col].map(lambda v: (f"{v:+.1f}" if signed else f"{v:.1f}") if pd.notna(v) else "--")
    table = table.rename(columns={
        "fixture": "Fixture", "comp": "Comp", "date": "Date", "side": "Side",
        "model_pct": "Model %", "bet365_pct": "Bet365 %", "kalshi_pct": "Kalshi %", "poly_pct": "Poly %",
        "pool_hit_pct": "Pool hit %", "edge_pp": "Edge (pp)", "verdict": "Verdict",
    })
    pd.set_option("display.width", 240)
    pd.set_option("display.max_colwidth", 60)
    print(table.to_string(index=False))


def print_picks_table(live_df: pd.DataFrame, xg_odds_model, xg_odds_scaler, calibrators: dict) -> None:
    """The standing 'give me picks' format, in two independent
    sections -- one per model, not merged into shared rows:

      1. xG+core -- the model actually used for live picks. A
         fixture+side gets a row whenever its OWN calibrated
         probability is >= TABLE_MIN_MODEL_P (60%), no bar-clearing
         gate on top of that. Edge/Pool hit % are the real, validated
         numbers (best_edge_over/_under, effective_over_prob/
         effective_under_prob) -- same as the rest of the pipeline.
         A league excluded from real picks (OVER_DISALLOWED_
         COMPETITIONS/UNDER_DISALLOWED_COMPETITIONS) still gets a row
         like any other, just tagged (thin data) with no pool hit
         rate attached (there isn't a validated one for it).

      2. xG+odds -- purely informational (MARKET_ODDS_ENABLED is off,
         so this cohort is never live), computed fresh here via the
         Bet365 bypass. No validated pool hit rate exists for this
         cohort at all, so its own edge is computed directly against
         Kalshi/Polymarket's fair value/ask (same fee-inclusive
         formula the live pipeline uses, just with this cohort's own
         probability standing in for the pool-substituted one).
    """
    core_rows = []
    xg_odds_rows = []
    for _, r in live_df.iterrows():
        comp = r["competition"]
        over_disallowed = comp in OVER_DISALLOWED_COMPETITIONS
        under_disallowed = comp in UNDER_DISALLOWED_COMPETITIONS
        bet365 = _fetch_bet365_fair(comp, r["home_team"], r["away_team"], r["date"][:10])

        # -- Section 1: xG+core, the live model --
        if r["calibrated_p"] >= TABLE_MIN_MODEL_P:
            edge = None if over_disallowed else (r["best_edge_over"] * 100 if pd.notna(r["best_edge_over"]) else None)
            verdict = "EDGE" if edge is not None and edge > 0 else "no edge"
            if over_disallowed:
                verdict += " (thin data)"
            core_rows.append({
                "fixture": f"{r['home_team']} vs {r['away_team']}", "comp": comp, "date": r["date"][:10],
                "side": "Over", "model_pct": r["calibrated_p"] * 100,
                "bet365_pct": bet365 * 100 if bet365 is not None else None,
                "kalshi_pct": r["kalshi_fair_p"] * 100 if pd.notna(r["kalshi_fair_p"]) else (r["kalshi_yes_ask"] * 100 if pd.notna(r["kalshi_yes_ask"]) else None),
                "poly_pct": r["poly_fair_p"] * 100 if pd.notna(r["poly_fair_p"]) else (r["poly_yes_ask"] * 100 if pd.notna(r["poly_yes_ask"]) else None),
                "pool_hit_pct": None if over_disallowed else r["effective_over_prob"] * 100,
                "edge_pp": edge, "verdict": verdict,
            })
        if r["under_p"] >= TABLE_MIN_MODEL_P:
            edge = None if under_disallowed else (r["best_edge_under"] * 100 if pd.notna(r["best_edge_under"]) else None)
            verdict = "EDGE" if edge is not None and edge > 0 else "no edge"
            if under_disallowed:
                verdict += " (thin data)"
            core_rows.append({
                "fixture": f"{r['home_team']} vs {r['away_team']}", "comp": comp, "date": r["date"][:10],
                "side": "Under", "model_pct": r["under_p"] * 100,
                "bet365_pct": (100 - bet365 * 100) if bet365 is not None else None,
                "kalshi_pct": (100 - r["kalshi_fair_p"] * 100) if pd.notna(r["kalshi_fair_p"]) else (r["kalshi_no_ask"] * 100 if pd.notna(r["kalshi_no_ask"]) else None),
                "poly_pct": (100 - r["poly_fair_p"] * 100) if pd.notna(r["poly_fair_p"]) else (r["poly_no_ask"] * 100 if pd.notna(r["poly_no_ask"]) else None),
                "pool_hit_pct": None if under_disallowed else r["effective_under_prob"] * 100,
                "edge_pp": edge, "verdict": verdict,
            })

        # -- Section 2: xG+odds, informational only --
        xg_odds_over = _xg_odds_prob(r, bet365, xg_odds_model, xg_odds_scaler, calibrators)
        if xg_odds_over is None:
            continue
        if xg_odds_over >= TABLE_MIN_MODEL_P:
            edge = None
            if pd.notna(r["kalshi_yes_ask"]):
                edge = (xg_odds_over - kalshi_fee(r["kalshi_yes_ask"]) - r["kalshi_yes_ask"]) * 100
            if pd.notna(r["poly_yes_ask"]):
                poly_edge = (xg_odds_over - polymarket_fee(r["poly_yes_ask"]) - r["poly_yes_ask"]) * 100
                edge = poly_edge if edge is None else max(edge, poly_edge)
            verdict = "EDGE" if edge is not None and edge > 0 else "no edge"
            xg_odds_rows.append({
                "fixture": f"{r['home_team']} vs {r['away_team']}", "comp": comp, "date": r["date"][:10],
                "side": "Over", "model_pct": xg_odds_over * 100,
                "bet365_pct": bet365 * 100,
                "kalshi_pct": r["kalshi_fair_p"] * 100 if pd.notna(r["kalshi_fair_p"]) else (r["kalshi_yes_ask"] * 100 if pd.notna(r["kalshi_yes_ask"]) else None),
                "poly_pct": r["poly_fair_p"] * 100 if pd.notna(r["poly_fair_p"]) else (r["poly_yes_ask"] * 100 if pd.notna(r["poly_yes_ask"]) else None),
                "pool_hit_pct": None, "edge_pp": edge, "verdict": verdict,
            })
        if (1 - xg_odds_over) >= TABLE_MIN_MODEL_P:
            under_p = 1 - xg_odds_over
            edge = None
            if pd.notna(r["kalshi_no_ask"]):
                edge = (under_p - kalshi_fee(r["kalshi_no_ask"]) - r["kalshi_no_ask"]) * 100
            if pd.notna(r["poly_no_ask"]):
                poly_edge = (under_p - polymarket_fee(r["poly_no_ask"]) - r["poly_no_ask"]) * 100
                edge = poly_edge if edge is None else max(edge, poly_edge)
            verdict = "EDGE" if edge is not None and edge > 0 else "no edge"
            xg_odds_rows.append({
                "fixture": f"{r['home_team']} vs {r['away_team']}", "comp": comp, "date": r["date"][:10],
                "side": "Under", "model_pct": under_p * 100,
                "bet365_pct": 100 - bet365 * 100,
                "kalshi_pct": (100 - r["kalshi_fair_p"] * 100) if pd.notna(r["kalshi_fair_p"]) else (r["kalshi_no_ask"] * 100 if pd.notna(r["kalshi_no_ask"]) else None),
                "poly_pct": (100 - r["poly_fair_p"] * 100) if pd.notna(r["poly_fair_p"]) else (r["poly_no_ask"] * 100 if pd.notna(r["poly_no_ask"]) else None),
                "pool_hit_pct": None, "edge_pp": edge, "verdict": verdict,
            })

    value_cols = ("model_pct", "bet365_pct", "kalshi_pct", "poly_pct", "pool_hit_pct", "edge_pp")
    print(f"\n{'='*100}\nXG+CORE (live model)\n{'='*100}")
    if not core_rows:
        print(f"Nothing at or above {TABLE_MIN_MODEL_P*100:.0f}% confidence this window.")
    else:
        _print_table(core_rows, value_cols)

    print(f"\n{'='*100}\nXG+ODDS (reference only -- not a live pick input)\n{'='*100}")
    if not xg_odds_rows:
        print(f"Nothing at or above {TABLE_MIN_MODEL_P*100:.0f}% confidence this window (or no Bet365 price available to compute it).")
    else:
        _print_table(xg_odds_rows, value_cols)


def explain_row(row: pd.Series, features: list[str], model, scaler: StandardScaler, top_n: int = 8) -> list[tuple]:
    x = scaler.transform(row[features].to_frame().T)[0]
    contributions = [(f, c * x[i]) for i, (f, c) in enumerate(zip(features, model.coef_[0])) if abs(c) > 1e-6]
    contributions.sort(key=lambda t: -abs(t[1]))
    return contributions[:top_n]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--top", type=int, default=10, help="Explain the top N fixtures (by Kalshi edge, unless --by-probability)")
    parser.add_argument("--fixture-id", type=int, default=None, help="Explain one specific fixture instead")
    parser.add_argument("--by-probability", action="store_true",
                         help="Rank confident picks by raw calibrated probability instead of Kalshi edge")
    parser.add_argument("--no-bar", action="store_true",
                         help="Skip the confidence-bar filter -- explore everything, not just real picks")
    parser.add_argument("--table", action="store_true",
                         help="Print one compact table (model/Bet365/Kalshi/Polymarket/pool hit rate/edge/verdict) "
                              "instead of the per-fixture explain report -- this is the standing 'give me picks' format.")
    args = parser.parse_args()

    cached = model_cache.load("explain_picks_bundle")
    if cached is not None:
        try:
            (bar, under_bar, early_bar, early_under_bar, pool_over, pool_over_early, pool_under,
             model, scaler, xg_model, xg_scaler, odds_model, odds_scaler,
             xg_odds_model, xg_odds_scaler, state) = cached
        except (ValueError, TypeError):
            # A code change altered the bundle's shape since this cache
            # file was written under the same data fingerprint -- treat
            # it as a miss and retrain, don't crash on a stale format.
            cached = None
    if cached is not None:
        print("Reusing cached models/bars/replayed state (data/*.csv unchanged since last run)...")
        print(f"  bar (Over)  = {bar*100:.1f}%  |  bar (Under) = {under_bar*100:.1f}%")
        print(f"  early-season bar (Over) = {early_bar*100:.1f}%  |  early-season bar (Under) = {early_under_bar*100:.1f}%  "
              f"(applies when either team has <= {EARLY_SEASON_CUTOFF} games played this season)")
        print(f"  pool hit rate used for edge: Over {pool_over[0]*100:.1f}% (n={pool_over[1]}), "
              f"Over/early-season {pool_over_early[0]*100:.1f}% (n={pool_over_early[1]}), "
              f"Under {pool_under[0]*100:.1f}% (n={pool_under[1]})")
    else:
        # Built ONCE and passed to every call below -- each of bar/
        # under_bar/early_bar/early_under_bar/pool_over/pool_over_early/
        # pool_under used to rebuild this from scratch internally (7x
        # redundant retrain of both models, the actual expensive part).
        print("Building the out-of-fold calibrated stream once (shared by every bar/pool computation below)...")
        shared_stream = _calibrated_stream()

        print("Computing the live confidence bar (rolling p95 of trailing historical predictions)...")
        bar = compute_rolling_p95_bar(stream=shared_stream)
        under_bar = compute_rolling_p95_under_bar(stream=shared_stream)
        print(f"  bar (Over)  = {bar*100:.1f}% -- only fixtures at or above this are real Over picks")
        print(f"  bar (Under) = {under_bar*100:.1f}% -- only fixtures at or above this are real Under picks "
              f"(weaker track record than Over -- see compute_rolling_p95_under_bar docstring)\n")

        print("Computing the stricter early-season bar (check_early_season_reliability.py found "
              f"+6.1pp overconfidence when either team has <= {EARLY_SEASON_CUTOFF} games played this season)...")
        early_bar = compute_rolling_p95_bar(early_season_only=True, stream=shared_stream)
        early_under_bar = compute_rolling_p95_under_bar(early_season_only=True, stream=shared_stream)
        print(f"  early-season bar (Over)  = {early_bar*100:.1f}%")
        print(f"  early-season bar (Under) = {early_under_bar*100:.1f}%\n")

        # Edge is priced off the POOL's historical hit rate, not any single
        # fixture's own calibrated_p -- see compute_pool_hit_rate()'s
        # docstring. check_confidence_bar_sweep.py's bucket breakdown found
        # ~zero correlation (r=0.082) between a bar-clearing pick's stated
        # confidence and its actual outcome, so trusting the specific
        # number claims precision the data doesn't support.
        print("Computing pool hit rates (what edge actually gets priced against)...")
        pool_over = compute_pool_hit_rate(under=False, early_season_only=False, stream=shared_stream)
        pool_over_early = compute_pool_hit_rate(under=False, early_season_only=True, stream=shared_stream)
        pool_under = compute_pool_hit_rate(under=True, early_season_only=False, stream=shared_stream)
        print(f"  Over pool hit rate = {pool_over[0]*100:.1f}% (n={pool_over[1]})")
        print(f"  Over/early-season pool hit rate = {pool_over_early[0]*100:.1f}% (n={pool_over_early[1]})")
        print(f"  Under pool hit rate = {pool_under[0]*100:.1f}% (n={pool_under[1]})")
        print(f"  (no early-season Under pool number -- that bar was found unreliable at every percentile "
              f"tested, so early-season fixtures are never offered as Under picks)\n")

        print(f"Training the core model ({TRAINING_DATA_CUTOFF} onward -- see that constant's docstring)...")
        historical = load_with_player_form_and_shots_venue()
        historical = historical[historical["date"] >= TRAINING_DATA_CUTOFF]
        model_df = historical[CORE_CANDIDATES + ["over_2_5"]].dropna()
        scaler = StandardScaler()
        X_train = scaler.fit_transform(model_df[CORE_CANDIDATES])
        model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
        model.fit(X_train, model_df["over_2_5"])

        print("Training the xG-augmented model...")
        xg_historical = load_with_xg_player_form_and_shots_venue()
        xg_historical = xg_historical[xg_historical["date"] >= TRAINING_DATA_CUTOFF]
        xg_historical = load_weighted_xg(xg_historical)
        xg_historical = load_team_ratings(xg_historical)
        xg_model_df = xg_historical[XG_CANDIDATES + ["over_2_5"]].dropna()
        xg_scaler = StandardScaler()
        X_xg_train = xg_scaler.fit_transform(xg_model_df[XG_CANDIDATES])
        xg_model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
        xg_model.fit(X_xg_train, xg_model_df["over_2_5"])

        # Two more independent tiers -- see market_odds_features.py's
        # docstring for the kill switch, and predict_upcoming.py's
        # ODDS_CANDIDATES/XG_ODDS_CANDIDATES comment for both
        # combinations' validated numbers (xG+odds improved more than
        # core+odds). odds_model/xg_odds_model are None when the odds
        # data file is empty/missing (e.g. right after
        # MARKET_ODDS_ENABLED was flipped off and the feature is being
        # retired) -- callers below must check for that.
        odds_model = odds_scaler = None
        xg_odds_model = xg_odds_scaler = None
        odds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market_odds_features.csv")
        if os.path.exists(odds_path):
            # The CSV may hold rows for leagues pulled for a bigger
            # validation pass but not yet confirmed/added to
            # COVERED_COMPETITIONS -- filter to fixture_ids in a
            # covered league so an uncovered league's data never
            # silently enters training before it's actually validated.
            odds_features_df_all = pd.read_csv(odds_path)
            covered_fixture_ids = set(historical.loc[historical["competition"].isin(COVERED_COMPETITIONS), "fixture_id"]) | \
                                   set(xg_historical.loc[xg_historical["competition"].isin(COVERED_COMPETITIONS), "fixture_id"])
            odds_features_df = odds_features_df_all[odds_features_df_all["fixture_id"].isin(covered_fixture_ids)]

            print("Training the odds-augmented model (core + Bet365 devigged Over 2.5)...")
            odds_historical = historical.merge(odds_features_df, on="fixture_id", how="inner")
            odds_model_df = odds_historical[ODDS_CANDIDATES + ["over_2_5"]].dropna()
            if len(odds_model_df) >= 200:
                odds_scaler = StandardScaler()
                X_odds_train = odds_scaler.fit_transform(odds_model_df[ODDS_CANDIDATES])
                odds_model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
                odds_model.fit(X_odds_train, odds_model_df["over_2_5"])
                print(f"  trained on {len(odds_model_df)} rows (COVERED_COMPETITIONS leagues only)")
            else:
                print(f"  only {len(odds_model_df)} rows with both core features and a market price -- skipping, too few to trust")

            print("Training the xG+odds model (xG-augmented + Bet365 devigged Over 2.5)...")
            xg_odds_historical = xg_historical.merge(odds_features_df, on="fixture_id", how="inner")
            xg_odds_model_df = xg_odds_historical[XG_ODDS_CANDIDATES + ["over_2_5"]].dropna()
            if len(xg_odds_model_df) >= 200:
                xg_odds_scaler = StandardScaler()
                X_xg_odds_train = xg_odds_scaler.fit_transform(xg_odds_model_df[XG_ODDS_CANDIDATES])
                xg_odds_model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
                xg_odds_model.fit(X_xg_odds_train, xg_odds_model_df["over_2_5"])
                print(f"  trained on {len(xg_odds_model_df)} rows (COVERED_COMPETITIONS leagues only)")
            else:
                print(f"  only {len(xg_odds_model_df)} rows with both xG features and a market price -- skipping, too few to trust")

        print("Replaying full match history to build current team state...")
        all_finished = fetch_all_fixtures(None)
        state = replay_to_current_state(all_finished)

        model_cache.save("explain_picks_bundle", (
            bar, under_bar, early_bar, early_under_bar, pool_over, pool_over_early, pool_under,
            model, scaler, xg_model, xg_scaler, odds_model, odds_scaler,
            xg_odds_model, xg_odds_scaler, state,
        ))

    # Cheap regardless of cache hit/miss -- apifootball's own on-disk cache
    # (ttl=None) makes this a near-instant reload when it was just fetched
    # above, and it's needed unconditionally for live games-into-season
    # lookups below.
    all_finished = fetch_all_fixtures(None)

    print("Fetching upcoming fixtures...")
    upcoming = fetch_upcoming_fixtures(args.days)
    if args.fixture_id is not None:
        upcoming = [m for m in upcoming if m["fixture_id"] == args.fixture_id]

    rows = []
    for m in upcoming:
        row = build_feature_row(m, state)
        if row is not None:
            rows.append(row)
    if not rows:
        print("No fixtures had enough team history to score.")
        return 0

    live_df = pd.DataFrame(rows)
    live_df = add_derived_features(live_df)
    live_df = add_weighted_xg_derived_features(live_df)
    live_df = add_geo_mean_features(live_df)
    live_df = add_ratings_derived_features(live_df)
    live_df = add_player_form_derived_features(live_df)
    live_df = add_shots_venue_derived_features(live_df)
    standings_cache = build_standings_cache()
    live_df = add_league_finish_features(live_df, standings_cache)
    live_df = live_df.dropna(subset=CORE_CANDIDATES)
    if live_df.empty:
        print("All fixtures were missing a required feature.")
        return 0

    has_xg = live_df[
        XG_FINISHING_FEATURES + WEIGHTED_XG_RAW_FEATURES + GEO_FEATURES
        + RATINGS_RAW_FEATURES + RATINGS_DERIVED_FEATURES
    ].notna().all(axis=1)
    live_df["has_xg"] = has_xg

    # Odds-augmented tiers: any fixture in a covered competition with a
    # live Bet365 price gets upgraded, whether it would otherwise use
    # core or xG -- see market_odds_features.py's docstring for the
    # kill switch and predict_upcoming.py's ODDS_CANDIDATES/
    # XG_ODDS_CANDIDATES comment for why xG+odds > core+odds > xG >
    # core is the priority order (strongest validated combination
    # wins). Never blocks/crashes when unavailable -- falls back a
    # step at a time.
    live_df["mkt_over25_prob"] = pd.NA
    odds_eligible = MARKET_ODDS_ENABLED and (odds_model is not None or xg_odds_model is not None)
    if odds_eligible:
        covered = live_df["competition"].isin(COVERED_COMPETITIONS)
        for idx in live_df.loc[covered].index:
            r = live_df.loc[idx]
            price = fetch_market_over25_prob(r["competition"], r["home_team"], r["away_team"], r["date"])
            if price is not None:
                live_df.loc[idx, "mkt_over25_prob"] = price
    has_odds = live_df["mkt_over25_prob"].notna()

    live_df["raw_p"] = pd.NA
    live_df["model_used"] = ""

    use_xg_odds = has_xg & has_odds & (xg_odds_model is not None)
    use_core_odds = (~has_xg) & has_odds & (odds_model is not None)
    use_xg = has_xg & ~use_xg_odds
    use_core = (~has_xg) & ~use_core_odds

    xg_odds_rows = live_df.loc[use_xg_odds]
    if not xg_odds_rows.empty:
        live_df.loc[xg_odds_rows.index, "raw_p"] = xg_odds_model.predict_proba(xg_odds_scaler.transform(xg_odds_rows[XG_ODDS_CANDIDATES]))[:, 1]
        live_df.loc[xg_odds_rows.index, "model_used"] = "xG+odds"
    odds_rows = live_df.loc[use_core_odds]
    if not odds_rows.empty:
        live_df.loc[odds_rows.index, "raw_p"] = odds_model.predict_proba(odds_scaler.transform(odds_rows[ODDS_CANDIDATES]))[:, 1]
        live_df.loc[odds_rows.index, "model_used"] = "core+odds"
    xg_rows = live_df.loc[use_xg]
    if not xg_rows.empty:
        live_df.loc[xg_rows.index, "raw_p"] = xg_model.predict_proba(xg_scaler.transform(xg_rows[XG_CANDIDATES]))[:, 1]
        live_df.loc[xg_rows.index, "model_used"] = "xG"
    core_rows = live_df.loc[use_core]
    if not core_rows.empty:
        live_df.loc[core_rows.index, "raw_p"] = model.predict_proba(scaler.transform(core_rows[CORE_CANDIDATES]))[:, 1]
        live_df.loc[core_rows.index, "model_used"] = "core"
    live_df["raw_p"] = live_df["raw_p"].astype(float)

    calibrators = load_calibrators()
    # "core+odds" and "xG+odds" reuse the "core"/"xG" calibrators
    # respectively -- same feature space plus one input, not a
    # separately-fitted calibration curve. An approximation, not a
    # full third/fourth calibration pipeline; documented here rather
    # than silently assumed.
    calibration_lookup = live_df["model_used"].replace({"core+odds": "core", "xG+odds": "xG"})
    live_df["calibrated_p"] = apply_calibration(live_df["raw_p"], calibration_lookup, calibrators)

    # Whichever of the two teams has played fewer games this
    # competition+season is the binding constraint -- a fixture is
    # "early season" if that minimum is <= EARLY_SEASON_CUTOFF, in which
    # case it has to clear the stricter early-season bar instead of the
    # regular one (see forward_test_log.py's EARLY_SEASON_CUTOFF docstring).
    live_df["home_games_into_season"] = live_df.apply(
        lambda r: team_games_into_season_live(r["home_team"], r["competition"], r["season"], all_finished), axis=1)
    live_df["away_games_into_season"] = live_df.apply(
        lambda r: team_games_into_season_live(r["away_team"], r["competition"], r["season"], all_finished), axis=1)
    live_df["min_games_into_season"] = live_df[["home_games_into_season", "away_games_into_season"]].min(axis=1)
    live_df["is_early_season"] = live_df["min_games_into_season"] <= EARLY_SEASON_CUTOFF
    live_df["effective_bar"] = live_df["is_early_season"].map({True: early_bar, False: bar})
    live_df["effective_under_bar"] = live_df["is_early_season"].map({True: early_under_bar, False: under_bar})

    live_df["clears_bar"] = live_df["calibrated_p"] >= live_df["effective_bar"]
    live_df["under_p"] = 1 - live_df["calibrated_p"]
    live_df["clears_under_bar"] = live_df["under_p"] >= live_df["effective_under_bar"]
    # See UNDER_DISALLOWED_COMPETITIONS' docstring -- a per-competition
    # tier check found LIGAMX's Under picks underperforming the shared
    # pool at every validated tier, despite Over picks tracking it fine.
    live_df.loc[live_df["competition"].isin(UNDER_DISALLOWED_COMPETITIONS), "clears_under_bar"] = False
    # See OVER_DISALLOWED_COMPETITIONS' docstring -- these leagues simply
    # don't have enough historical Over picks yet to trust either way.
    live_df.loc[live_df["competition"].isin(OVER_DISALLOWED_COMPETITIONS), "clears_bar"] = False

    # Edge gets priced off the POOL's historical hit rate, not this
    # fixture's own calibrated_p -- see compute_pool_hit_rate()'s
    # docstring for why (essentially zero correlation between a
    # bar-clearing pick's individual stated confidence and its actual
    # outcome). calibrated_p/under_p are still shown for context, but the
    # trading decision uses effective_over_prob/effective_under_prob.
    #
    # CRITICAL: the pool number is only a valid substitute for a fixture
    # that actually CLEARS the corresponding bar -- it's the average
    # outcome of "things confident enough to already be in this pool",
    # not a universal constant. Applying it to a fixture nowhere near
    # that pool (e.g. a fixture the model rates 19% Under, priced
    # accordingly by Kalshi at a cheap no_ask) manufactures a nonsense
    # "edge" out of the gap between an irrelevant flat number and a cheap
    # price. Fixtures that don't clear a bar fall back to their own
    # calibrated_p/under_p for display -- not independently validated
    # to the same standard, but at least not actively misleading, and
    # they were never going to become picks either way (is_pick/
    # is_under_pick already gate on clears_bar/clears_under_bar below).
    live_df["effective_over_prob"] = live_df["calibrated_p"].where(
        ~live_df["clears_bar"], live_df["is_early_season"].map({True: pool_over_early[0], False: pool_over[0]})
    )
    # Under pool substitution additionally requires NOT early-season --
    # no validated early-season Under pool exists, so even a fixture that
    # clears the (separately-computed) early-season Under bar falls back
    # to its own under_p for display, same as never clearing at all.
    under_pool_applies = live_df["clears_under_bar"] & ~live_df["is_early_season"]
    live_df["effective_under_prob"] = live_df["under_p"].where(~under_pool_applies, pool_under[0])

    print("\nFetching real Kalshi prices per league...")
    kalshi_by_comp = {}
    for comp, series in KALSHI_SERIES_BY_COMPETITION.items():
        try:
            kalshi_by_comp[comp] = fetch_kalshi_over25_for_series(series)
        except Exception as exc:
            print(f"  {comp} ({series}): could not reach Kalshi -- {exc}")
            kalshi_by_comp[comp] = []

    live_df["kalshi_yes_ask"] = pd.NA
    live_df["edge_vs_ask"] = pd.NA
    live_df["kalshi_no_ask"] = pd.NA
    live_df["edge_no"] = pd.NA
    live_df["kalshi_fair_p"] = pd.NA
    live_df["edge_vs_fair"] = pd.NA
    # Fee is paid on entry regardless of outcome (see kalshi_fee()'s
    # docstring) -- subtracted here so edge reflects what a real trade
    # actually nets, same as forward_test_log.py's cmd_snapshot/settle.
    # ~$0.02/contract at these price levels, ~2pp of edge -- enough on
    # its own to flip a thin pick negative.
    for idx, r in live_df.iterrows():
        for k in kalshi_by_comp.get(r["competition"], []):
            if _normalize(k["home"]) == _normalize(r["home_team"]) and _normalize(k["away"]) == _normalize(r["away_team"]):
                live_df.at[idx, "kalshi_yes_ask"] = k["yes_ask"]
                yes_fee = kalshi_fee(k["yes_ask"])
                live_df.at[idx, "edge_vs_ask"] = r["effective_over_prob"] - yes_fee - k["yes_ask"]
                if k.get("no_ask") is not None:
                    live_df.at[idx, "kalshi_no_ask"] = k["no_ask"]
                    no_fee = kalshi_fee(k["no_ask"])
                    live_df.at[idx, "edge_no"] = r["effective_under_prob"] - no_fee - k["no_ask"]
                if k.get("fair_p") is not None:
                    live_df.at[idx, "kalshi_fair_p"] = k["fair_p"]
                    live_df.at[idx, "edge_vs_fair"] = r["effective_over_prob"] - yes_fee - k["fair_p"]
                break

    print("Fetching real Polymarket prices per league...")
    poly_by_comp = {}
    for comp in POLYMARKET_TAG_BY_COMPETITION:
        try:
            poly_by_comp[comp] = fetch_polymarket_over25_for_league(comp)
        except Exception as exc:
            print(f"  {comp}: could not reach Polymarket -- {exc}")
            poly_by_comp[comp] = []

    # Second, independent book -- same edge formula, same fee-on-entry
    # treatment (polymarket_fee(), Polymarket's own disclosed schedule),
    # kept in separate columns rather than overwriting Kalshi's so a
    # fixture priced on both books shows both. Matched with
    # _poly_names_match(), not live_kalshi_edge_test._normalize() --
    # Polymarket's official names (e.g. "AFC Bournemouth", "Bayer 04
    # Leverkusen") need different noise-stripping than Kalshi's
    # truncated ones, and running Kalshi's normalizer against them
    # would silently miss most of them.
    live_df["poly_yes_ask"] = pd.NA
    live_df["edge_vs_ask_poly"] = pd.NA
    live_df["poly_no_ask"] = pd.NA
    live_df["edge_no_poly"] = pd.NA
    live_df["poly_fair_p"] = pd.NA
    live_df["edge_vs_fair_poly"] = pd.NA
    for idx, r in live_df.iterrows():
        for k in poly_by_comp.get(r["competition"], []):
            if _poly_names_match(r["home_team"], k["home"]) and _poly_names_match(r["away_team"], k["away"]):
                live_df.at[idx, "poly_yes_ask"] = k["yes_ask"]
                yes_fee = polymarket_fee(k["yes_ask"])
                live_df.at[idx, "edge_vs_ask_poly"] = r["effective_over_prob"] - yes_fee - k["yes_ask"]
                if k.get("no_ask") is not None:
                    live_df.at[idx, "poly_no_ask"] = k["no_ask"]
                    no_fee = polymarket_fee(k["no_ask"])
                    live_df.at[idx, "edge_no_poly"] = r["effective_under_prob"] - no_fee - k["no_ask"]
                if k.get("fair_p") is not None:
                    live_df.at[idx, "poly_fair_p"] = k["fair_p"]
                    live_df.at[idx, "edge_vs_fair_poly"] = r["effective_over_prob"] - yes_fee - k["fair_p"]
                break

    live_df["priced"] = live_df["kalshi_yes_ask"].notna() | live_df["poly_yes_ask"].notna()
    # Only ONE book needs to show a profitable edge for a real pick --
    # two independently-run markets are extremely unlikely to be wrong
    # about the same fixture in the same direction for the same reason,
    # so requiring both would just mean missing real edge whenever one
    # book hasn't caught up to the other yet. best_edge_over/_under (the
    # actual tradeable number reported) is whichever book is better;
    # which book(s) actually cleared zero is tracked separately so the
    # report can say exactly where the edge is.
    live_df["best_edge_over"] = live_df[["edge_vs_ask", "edge_vs_ask_poly"]].apply(pd.to_numeric, errors="coerce").max(axis=1, skipna=True)
    live_df["best_edge_under"] = live_df[["edge_no", "edge_no_poly"]].apply(pd.to_numeric, errors="coerce").max(axis=1, skipna=True)
    live_df["is_pick"] = live_df["clears_bar"] & live_df["priced"] & (live_df["best_edge_over"] > 0)
    # Mirror for Under, using its own (weaker) validated bar -- see
    # compute_rolling_p95_under_bar(). Same gate, opposite side.
    # Early-season fixtures are EXCLUDED from Under picks entirely -- there
    # is no validated early-season Under pool number (every percentile
    # tested was unreliable, see compute_pool_hit_rate()'s docstring), so
    # there's nothing trustworthy to price the edge against.
    live_df["is_under_pick"] = (
        live_df["clears_under_bar"] & live_df["priced"] & (live_df["best_edge_under"] > 0) & (~live_df["is_early_season"])
    )
    live_df["best_edge"] = live_df[["best_edge_over", "best_edge_under"]].apply(pd.to_numeric, errors="coerce").max(axis=1, skipna=True)

    if args.table:
        print_picks_table(live_df, xg_odds_model, xg_odds_scaler, calibrators)
        return 0

    pool = live_df if (args.no_bar or args.fixture_id is not None) else live_df[live_df["clears_bar"] | live_df["clears_under_bar"]]
    if pool.empty:
        print(f"\nNo fixtures clear the Over bar ({bar*100:.1f}%) or the Under bar ({under_bar*100:.1f}%) in this window. "
              f"Pass --no-bar to explore everything anyway.")
        return 0

    if args.by_probability:
        ranked = pool.sort_values("calibrated_p", ascending=False)
    else:
        priced = pool.dropna(subset=["best_edge"])
        if priced.empty:
            print(f"\n{len(pool)} fixture(s) clear a confidence bar, but NONE have a real Kalshi price yet "
                  f"(too far from kickoff) -- nothing here is a recommendation without a real price to compare "
                  f"against. Showing them ranked by probability for reference only; re-run closer to kickoff.")
            ranked = pool.sort_values("calibrated_p", ascending=False)
        else:
            ranked = priced.sort_values("best_edge", ascending=False)

    if args.fixture_id is None:
        ranked = ranked.head(args.top)

    for _, r in ranked.iterrows():
        home, away = r["home_team"], r["away_team"]
        if r["model_used"] == "xG+odds":
            features, m, s = XG_ODDS_CANDIDATES, xg_odds_model, xg_odds_scaler
        elif r["model_used"] == "xG":
            features, m, s = XG_CANDIDATES, xg_model, xg_scaler
        elif r["model_used"] == "core+odds":
            features, m, s = ODDS_CANDIDATES, odds_model, odds_scaler
        else:
            features, m, s = CORE_CANDIDATES, model, scaler
        contributions = explain_row(r, features, m, s)

        print("\n" + "=" * 90)
        early_note = f", early-season bar: min {int(r['min_games_into_season'])} games played" if r["is_early_season"] else ""
        if r["is_pick"]:
            tag = f"PICK (Over{early_note})"
        elif r["is_under_pick"]:
            tag = f"PICK (Under, weaker track record -- see docs{early_note})"
        elif not r["clears_bar"] and not r["clears_under_bar"]:
            tag = (f"below both bars (Over {r['effective_bar']*100:.1f}% / Under {r['effective_under_bar']*100:.1f}%"
                   f"{early_note}), reference only")
        elif not r["priced"]:
            tag = f"confident, but no price on either book yet -- not a recommendation{early_note}"
        else:
            tag = f"confident, but negative edge on both books -- not a recommendation{early_note}"
        print(f"[{tag}]  {home} vs {away}  ({r['date']}, {r['competition']})")
        print(f"P(over 2.5) = {r['calibrated_p']*100:.1f}% calibrated  (raw {r['raw_p']*100:.1f}%, [{r['model_used']}] model)"
              f"   |   P(under 2.5) = {r['under_p']*100:.1f}%  -- individual model estimate, shown for context only")
        print(f"Edge is priced off the POOL hit rate, not the number above: Over {r['effective_over_prob']*100:.1f}% "
              f"/ Under {r['effective_under_prob']*100:.1f}% -- see compute_pool_hit_rate() docstring")
        if pd.notna(r["kalshi_yes_ask"]):
            print(f"Kalshi yes ask = ${r['kalshi_yes_ask']:.2f}  |  edge vs ask (Over, tradeable) = {r['edge_vs_ask']*100:+.1f}pp")
            if pd.notna(r["kalshi_no_ask"]):
                print(f"Kalshi no ask  = ${r['kalshi_no_ask']:.2f}  |  edge vs ask (Under, tradeable) = {r['edge_no']*100:+.1f}pp")
            if pd.notna(r["kalshi_fair_p"]):
                print(f"Kalshi de-vigged fair value = {r['kalshi_fair_p']*100:.1f}%  |  "
                      f"edge vs fair (true signal) = {r['edge_vs_fair']*100:+.1f}pp")
        else:
            print("No Kalshi price posted yet for this fixture.")
        if pd.notna(r["poly_yes_ask"]):
            print(f"Polymarket yes ask = ${r['poly_yes_ask']:.2f}  |  edge vs ask (Over, tradeable) = {r['edge_vs_ask_poly']*100:+.1f}pp")
            if pd.notna(r["poly_no_ask"]):
                print(f"Polymarket no ask  = ${r['poly_no_ask']:.2f}  |  edge vs ask (Under, tradeable) = {r['edge_no_poly']*100:+.1f}pp")
            if pd.notna(r["poly_fair_p"]):
                print(f"Polymarket fair value = {r['poly_fair_p']*100:.1f}%  |  "
                      f"edge vs fair (true signal) = {r['edge_vs_fair_poly']*100:+.1f}pp")
        else:
            print("No Polymarket price posted yet for this fixture (or team names didn't confidently match -- see polymarket_prices.py).")
        print("-" * 90)
        print("Top factors driving this prediction (ranked by how much they move the number):")
        for feat, contrib in contributions:
            direction = "pushes UP" if contrib > 0 else "pushes DOWN"
            desc = _describe(feat, home, away)
            raw_val = r[feat]
            print(f"  {direction:<12s} {desc}")
            print(f"    -> raw value: {raw_val:.2f}   |   contribution to log-odds: {contrib:+.3f}")

    # One combined list: real PICKs (clears the bar, priced, positive
    # edge) union'd with the WATCHLIST (model >= 60%, within 2pp of the
    # Kalshi price regardless of edge sign -- model/market agreement is
    # worth seeing on its own, not just cases where they disagree).
    # Scanned over the full fetch window, not just --top N or the ranked
    # subset, so nothing in either category gets truncated away.
    real_picks = live_df[live_df["is_pick"]]
    under_picks = live_df[live_df["is_under_pick"]]
    watch = live_df[
        live_df["priced"]
        & (live_df["calibrated_p"] >= WATCHLIST_MIN_P)
        & (live_df["edge_vs_ask"].abs() <= WATCHLIST_MAX_ABS_EDGE)
    ]
    combined = pd.concat([real_picks, under_picks, watch]).drop_duplicates(subset=["fixture_id"])
    combined = combined.sort_values("best_edge", ascending=False)

    print("\n" + "=" * 90)
    print("VERDICT")
    print("=" * 90)
    if combined.empty:
        print(f"Nothing this window is either a real pick (Over >= {bar*100:.1f}% or Under >= {under_bar*100:.1f}%, "
              f"priced on Kalshi and/or Polymarket, positive edge on at least one) or close to the market "
              f"(model >= {WATCHLIST_MIN_P*100:.0f}%, within {WATCHLIST_MAX_ABS_EDGE*100:.0f}pp of Kalshi). "
              f"Re-check closer to kickoff.")
    else:
        for _, r in combined.iterrows():
            early_note = f", early-season bar (min {int(r['min_games_into_season'])} games played)" if r["is_early_season"] else ""
            if r["is_pick"]:
                label = f"PICK (Over{early_note})"
                kalshi_price, kalshi_edge = r["kalshi_yes_ask"], r["edge_vs_ask"]
                poly_price, poly_edge = r["poly_yes_ask"], r["edge_vs_ask_poly"]
            elif r["is_under_pick"]:
                label = f"PICK (Under, weaker track record{early_note})"
                kalshi_price, kalshi_edge = r["kalshi_no_ask"], r["edge_no"]
                poly_price, poly_edge = r["poly_no_ask"], r["edge_no_poly"]
            else:
                label = f"WATCH (model/market agree, no edge{early_note})"
                kalshi_price, kalshi_edge = r["kalshi_yes_ask"], r["edge_vs_ask"]
                poly_price, poly_edge = r["poly_yes_ask"], r["edge_vs_ask_poly"]
            # Report whichever book(s) actually have a price -- a pick can
            # be real on the strength of just one book's edge even when
            # the other has no price at all, so never assume both exist.
            book_strs = []
            if pd.notna(kalshi_price):
                cleared = " *edge here*" if pd.notna(kalshi_edge) and kalshi_edge > 0 else ""
                book_strs.append(f"Kalshi ${kalshi_price:.2f} ({kalshi_edge*100:+.1f}pp{cleared})")
            if pd.notna(poly_price):
                cleared = " *edge here*" if pd.notna(poly_edge) and poly_edge > 0 else ""
                book_strs.append(f"Polymarket ${poly_price:.2f} ({poly_edge*100:+.1f}pp{cleared})")
            books_str = " | ".join(book_strs) if book_strs else "no price on either book"
            print(f"  [{label}] {r['home_team']} vs {r['away_team']} ({r['competition']}, {r['date']}) -- "
                  f"model {r['calibrated_p']*100:.1f}% over / {r['under_p']*100:.1f}% under (individual estimate) "
                  f"vs {books_str} (priced off pool hit rate "
                  f"{r['effective_over_prob']*100:.1f}%/{r['effective_under_prob']*100:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
