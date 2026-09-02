#!/usr/bin/env python3
"""What are the actual picks, and why? This is the single authoritative
answer -- not a raw data dump you have to reconcile against another
tool's output yourself.

A fixture only counts as a real pick if it clears BOTH bars:
  1. The model is genuinely confident -- its calibrated probability
     clears the rolling p95 bar (same live selection rule
     forward_test_log.py uses, computed fresh from the model's own
     trailing historical predictions, not picked after the fact).
  2. There's a real Kalshi price, and the model disagrees with it in
     the profitable direction (positive edge).

A big "edge" on a fixture the model itself only rates a coin flip is
NOT a real pick -- it just means Kalshi's price is even more extreme
than a so-so model number, which is a much weaker signal. Only
fixtures clearing the confidence bar are ever shown as picks; --no-bar
shows everything for exploration, clearly separated from the real list.

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
import warnings

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import add_derived_features
from analyze_player_form import add_player_form_derived_features
from analyze_shots_venue import (
    add_shots_venue_derived_features,
    load_with_player_form_and_shots_venue,
    load_with_xg_player_form_and_shots_venue,
)
from analyze_xg_features import XG_RAW_FEATURES, XG_DERIVED_FEATURES, add_xg_derived_features
from calibration import apply_calibration, load_calibrators
from forward_test_log import KALSHI_SERIES_BY_COMPETITION, compute_rolling_p95_bar, fetch_kalshi_over25_for_series
from live_kalshi_edge_test import _normalize
from predict_upcoming import (
    CORE_CANDIDATES,
    XG_CANDIDATES,
    build_feature_row,
    fetch_upcoming_fixtures,
    replay_to_current_state,
)
from build_dataset_apifootball import fetch_all_fixtures

warnings.filterwarnings("ignore")

# Plain-English descriptions for the features that actually tend to
# survive L1 selection, keyed on the RAW feature name with {home}/{away}
# placeholders swapped in by _describe(). Anything not in this dict
# just prints its raw name -- better an ugly label than a wrong one.
DESCRIPTIONS = {
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
    "home_finishing_last5": "{home}'s actual goals minus xG, last 5 (over/under-performing their chances)",
    "away_finishing_last5": "{away}'s actual goals minus xG, last 5",
    "season_year": "the calendar year itself -- the model has learned recent seasons trend higher-scoring than older ones",
    "min_competition_experience": "how established the less-tenured of the two teams is in this competition",
}


def _describe(feat: str, home: str, away: str) -> str:
    template = DESCRIPTIONS.get(feat)
    if template is None:
        return feat
    return template.format(home=home, away=away)


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
    args = parser.parse_args()

    print("Computing the live confidence bar (rolling p95 of trailing historical predictions)...")
    bar = compute_rolling_p95_bar()
    print(f"  bar = {bar*100:.1f}% -- only fixtures at or above this are real picks\n")

    print("Training the core model...")
    historical = load_with_player_form_and_shots_venue()
    model_df = historical[CORE_CANDIDATES + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X_train = scaler.fit_transform(model_df[CORE_CANDIDATES])
    model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    model.fit(X_train, model_df["over_2_5"])

    print("Training the xG-augmented model...")
    xg_historical = load_with_xg_player_form_and_shots_venue()
    xg_model_df = xg_historical[XG_CANDIDATES + ["over_2_5"]].dropna()
    xg_scaler = StandardScaler()
    X_xg_train = xg_scaler.fit_transform(xg_model_df[XG_CANDIDATES])
    xg_model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    xg_model.fit(X_xg_train, xg_model_df["over_2_5"])

    print("Fetching upcoming fixtures...")
    upcoming = fetch_upcoming_fixtures(args.days)
    if args.fixture_id is not None:
        upcoming = [m for m in upcoming if m["fixture_id"] == args.fixture_id]
    all_finished = fetch_all_fixtures(None)
    state = replay_to_current_state(all_finished)

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
    live_df = add_xg_derived_features(live_df)
    live_df = add_player_form_derived_features(live_df)
    live_df = add_shots_venue_derived_features(live_df)
    live_df = live_df.dropna(subset=CORE_CANDIDATES)
    if live_df.empty:
        print("All fixtures were missing a required feature.")
        return 0

    has_xg = live_df[XG_RAW_FEATURES + XG_DERIVED_FEATURES].notna().all(axis=1)
    live_df["raw_p"] = pd.NA
    live_df["model_used"] = ""
    core_rows = live_df.loc[~has_xg]
    if not core_rows.empty:
        live_df.loc[~has_xg, "raw_p"] = model.predict_proba(scaler.transform(core_rows[CORE_CANDIDATES]))[:, 1]
        live_df.loc[~has_xg, "model_used"] = "core"
    xg_rows = live_df.loc[has_xg]
    if not xg_rows.empty:
        live_df.loc[has_xg, "raw_p"] = xg_model.predict_proba(xg_scaler.transform(xg_rows[XG_CANDIDATES]))[:, 1]
        live_df.loc[has_xg, "model_used"] = "xG"
    live_df["raw_p"] = live_df["raw_p"].astype(float)

    calibrators = load_calibrators()
    live_df["calibrated_p"] = apply_calibration(live_df["raw_p"], live_df["model_used"], calibrators)
    live_df["is_pick"] = live_df["calibrated_p"] >= bar

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
    for idx, r in live_df.iterrows():
        for k in kalshi_by_comp.get(r["competition"], []):
            if _normalize(k["home"]) == _normalize(r["home_team"]) and _normalize(k["away"]) == _normalize(r["away_team"]):
                live_df.at[idx, "kalshi_yes_ask"] = k["yes_ask"]
                live_df.at[idx, "edge_vs_ask"] = r["calibrated_p"] - k["yes_ask"]
                break

    pool = live_df if (args.no_bar or args.fixture_id is not None) else live_df[live_df["is_pick"]]
    if pool.empty:
        print(f"\nNo fixtures clear the {bar*100:.1f}% confidence bar in this window. "
              f"Pass --no-bar to explore everything anyway.")
        return 0

    if args.by_probability:
        ranked = pool.sort_values("calibrated_p", ascending=False)
    else:
        priced = pool.dropna(subset=["edge_vs_ask"])
        if priced.empty:
            print(f"\n{len(pool)} fixture(s) clear the confidence bar, but NONE have a real Kalshi price yet "
                  f"(too far from kickoff). Showing them ranked by probability instead; re-run closer to "
                  f"kickoff for real edge numbers.")
            ranked = pool.sort_values("calibrated_p", ascending=False)
        else:
            ranked = priced.sort_values("edge_vs_ask", ascending=False)

    if args.fixture_id is None:
        ranked = ranked.head(args.top)

    for _, r in ranked.iterrows():
        home, away = r["home_team"], r["away_team"]
        features = XG_CANDIDATES if r["model_used"] == "xG" else CORE_CANDIDATES
        m, s = (xg_model, xg_scaler) if r["model_used"] == "xG" else (model, scaler)
        contributions = explain_row(r, features, m, s)

        print("\n" + "=" * 90)
        tag = "PICK" if r["is_pick"] else f"below bar ({bar*100:.1f}%), reference only"
        print(f"[{tag}]  {home} vs {away}  ({r['date']}, {r['competition']})")
        print(f"P(over 2.5) = {r['calibrated_p']*100:.1f}% calibrated  (raw {r['raw_p']*100:.1f}%, [{r['model_used']}] model)")
        if pd.notna(r["kalshi_yes_ask"]):
            print(f"Kalshi ask = ${r['kalshi_yes_ask']:.2f}  |  edge = {r['edge_vs_ask']*100:+.1f}pp")
        else:
            print("No Kalshi price posted yet for this fixture.")
        print("-" * 90)
        print("Top factors driving this prediction (ranked by how much they move the number):")
        for feat, contrib in contributions:
            direction = "pushes UP" if contrib > 0 else "pushes DOWN"
            desc = _describe(feat, home, away)
            raw_val = r[feat]
            print(f"  {direction:<12s} {desc}")
            print(f"    -> raw value: {raw_val:.2f}   |   contribution to log-odds: {contrib:+.3f}")

    real_picks = ranked[ranked["is_pick"]]
    priced_picks = real_picks.dropna(subset=["edge_vs_ask"])
    positive_edge = priced_picks[priced_picks["edge_vs_ask"] > 0]
    print("\n" + "=" * 90)
    print("VERDICT")
    print("=" * 90)
    if positive_edge.empty:
        print(f"None of the fixtures shown above are both confident (>= {bar*100:.1f}%) AND priced with a "
              f"positive Kalshi edge right now. Nothing here is a real pick yet -- re-check closer to kickoff.")
    else:
        for _, r in positive_edge.iterrows():
            print(f"  PICK: {r['home_team']} vs {r['away_team']} ({r['competition']}, {r['date']}) -- "
                  f"model {r['calibrated_p']*100:.1f}% vs Kalshi ${r['kalshi_yes_ask']:.2f}, edge {r['edge_vs_ask']*100:+.1f}pp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
