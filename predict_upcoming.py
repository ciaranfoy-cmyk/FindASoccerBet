#!/usr/bin/env python3
"""Score upcoming (not-yet-played) fixtures with the validated over-2.5
model, and rank them — this is the actual "what should I look at next
week" tool.

Rebuilds each team's current rolling stats (goals, shots, clean sheets,
table position, etc.) by replaying all finished historical matches up to
today — same logic as build_dataset_apifootball.py, reused via import so
a live prediction is computed identically to how the model was trained.
Then fetches actual upcoming fixtures, pulls live injury/team-news for
each, and scores them with a model trained on the FULL historical
dataset (not the 80% split used for validation — that split's job was to
prove the approach works; a live model should use every match available).

Also trains a second, xG-augmented model (see build_xg_features.py /
rolling_validation_xg.py) on the smaller but real-xG-covered recent-era
subset. Since the current season falls entirely within that coverage
window, live fixtures almost always have real rolling xG available —
when they do, the xG model scores them (it's the stronger, though less
long-proven, of the two); when they don't (e.g. very early in a newly
promoted team's tracked history), falls back to the core model.

Both models use player-form (rolling goals-per-start of today's actual
starting attackers) in place of team-goals-form -- validated to beat it
in every fold, with or without xG present (rolling_validation_player_form_v2.py,
rolling_validation_xg_player_form_v2.py). Since this tool normally runs
days ahead of kickoff, before the real lineup is confirmed, it falls
back to a "usual XI" proxy (each team's most-frequent recent starters)
and only uses the real confirmed lineup when it's already been posted
(within ~1hr of kickoff).

Usage:
    APIFOOTBALL_KEY=xxxx python3 predict_upcoming.py
    APIFOOTBALL_KEY=xxxx python3 predict_upcoming.py --days 14
"""

import argparse
import datetime
import sys
import warnings
from collections import defaultdict, deque

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_dataset_apifootball import ALL_CANDIDATES, add_derived_features
from analyze_player_form import (
    PLAYER_FORM_DERIVED_FEATURES,
    PLAYER_FORM_RAW_FEATURES,
    TEAM_GOALS_FORM,
    add_player_form_derived_features,
    load_with_player_form,
    load_with_xg_and_player_form,
)
from analyze_xg_features import XG_DERIVED_FEATURES, XG_RAW_FEATURES, add_xg_derived_features, load_with_xg
from build_dataset_apifootball import (
    LEAGUES,
    clean_sheet_pct,
    fetch_all_fixtures,
    injury_count_for,
    rolling_avg,
    season_avg,
    shot_stats_for,
    table_standing,
)
from build_lineup_features import USUAL_XI_MIN_HISTORY, USUAL_XI_WINDOW, goal_scorers_for, lineup_for
from build_player_form_features import ATTACKING_POS as FORM_ATTACKING_POS
from build_player_form_features import MIN_STARTS_FOR_FORM
from build_player_form_features import ROLLING_WINDOW as FORM_ROLLING_WINDOW
from build_xg_features import MIN_GAMES_FOR_ROLLING as XG_MIN_GAMES
from build_xg_features import ROLLING_N as XG_ROLLING_N
from build_xg_features import xg_stats_for

warnings.filterwarnings("ignore")

# team-goals-form (home_gf_last5 etc.) replaced by player-form everywhere --
# validated in rolling_validation_player_form_v2.py (beats team-goals-form
# in every fold when xG isn't available) and rolling_validation_xg_player_form_v2.py
# (ties-or-beats it in every fold when xG IS available too, never worse).
CORE_CANDIDATES = [f for f in ALL_CANDIDATES if f not in TEAM_GOALS_FORM] + PLAYER_FORM_RAW_FEATURES + PLAYER_FORM_DERIVED_FEATURES
XG_CANDIDATES = CORE_CANDIDATES + XG_RAW_FEATURES + XG_DERIVED_FEATURES


def new_state() -> dict:
    return {
        "team_history": defaultdict(lambda: deque()),
        "team_shot_history": defaultdict(lambda: deque()),
        "team_competition_games": defaultdict(int),
        "team_last_played": {},
        "h2h_history": defaultdict(list),
        "table": defaultdict(dict),
        "xg_for_history": defaultdict(lambda: deque(maxlen=XG_ROLLING_N)),
        "xg_against_history": defaultdict(lambda: deque(maxlen=XG_ROLLING_N)),
        "xg_finishing_history": defaultdict(lambda: deque(maxlen=XG_ROLLING_N)),
        "team_lineup_history": defaultdict(lambda: deque(maxlen=USUAL_XI_WINDOW)),  # team_id -> [set(starter pids), ...]
        "player_goal_history": defaultdict(lambda: deque(maxlen=FORM_ROLLING_WINDOW)),  # player_id -> [goals per start, ...]
        "player_positions": {},  # player_id -> last known position
    }


def rolling_avg_xg(dq: deque) -> float | None:
    if len(dq) < XG_MIN_GAMES:
        return None
    return sum(dq) / len(dq)


def apply_match(state: dict, m: dict) -> None:
    """Fold one finished match into the tracked state — the same
    bookkeeping build_dataset_apifootball.main() does per row, extracted
    so a walk-forward backtest can advance the state incrementally
    (week by week) instead of replaying full history from scratch at
    every step.
    """
    date = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
    home, away = m["home"], m["away"]
    home_goals, away_goals = m["home_goals"], m["away_goals"]
    competition = m["competition"]
    season_label = f"{competition}-{m['season']}"

    try:
        shots = shot_stats_for(m["fixture_id"])
    except apifootball.ApiFootballError:
        shots = {}
    home_shots_this = shots.get(m["home_id"])
    away_shots_this = shots.get(m["away_id"])

    try:
        xg = xg_stats_for(m["fixture_id"])
    except apifootball.ApiFootballError:
        xg = {}
    home_id, away_id = m.get("home_id"), m.get("away_id")
    if home_id in xg and away_id in xg:
        home_xg, away_xg = xg[home_id]["xg"], xg[away_id]["xg"]
        state["xg_for_history"][home].append(home_xg)
        state["xg_against_history"][home].append(away_xg)
        state["xg_for_history"][away].append(away_xg)
        state["xg_against_history"][away].append(home_xg)
        state["xg_finishing_history"][home].append(home_goals - home_xg)
        state["xg_finishing_history"][away].append(away_goals - away_xg)

    try:
        lineups = lineup_for(m["fixture_id"])
    except apifootball.ApiFootballError:
        lineups = {}
    try:
        scorers = goal_scorers_for(m["fixture_id"])
    except apifootball.ApiFootballError:
        scorers = []
    goals_this_match: dict[int, int] = defaultdict(int)
    for _team_id, pid in scorers:
        goals_this_match[pid] += 1
    for team_id, lineup in ((home_id, lineups.get(home_id)), (away_id, lineups.get(away_id))):
        if not lineup:
            continue
        starter_ids = {pid for pid, pos, name in lineup}
        state["team_lineup_history"][team_id].append(starter_ids)
        for pid, pos, name in lineup:
            state["player_positions"][pid] = pos
            if pos in FORM_ATTACKING_POS:
                state["player_goal_history"][pid].append(goals_this_match.get(pid, 0))

    state["team_history"][home].append({"gf": home_goals, "ga": away_goals, "season_label": season_label})
    state["team_history"][away].append({"gf": away_goals, "ga": home_goals, "season_label": season_label})
    if home_shots_this:
        state["team_shot_history"][home].append(home_shots_this)
    if away_shots_this:
        state["team_shot_history"][away].append(away_shots_this)
    state["team_competition_games"][(home, competition)] += 1
    state["team_competition_games"][(away, competition)] += 1
    state["team_last_played"][home] = date
    state["team_last_played"][away] = date
    state["h2h_history"][tuple(sorted([home, away]))].append(home_goals + away_goals)

    season_table = state["table"][season_label]
    home_row = season_table.setdefault(home, {"points": 0, "played": 0, "gf": 0, "ga": 0})
    away_row = season_table.setdefault(away, {"points": 0, "played": 0, "gf": 0, "ga": 0})
    home_row["played"] += 1
    away_row["played"] += 1
    home_row["gf"] += home_goals
    home_row["ga"] += away_goals
    away_row["gf"] += away_goals
    away_row["ga"] += home_goals
    if home_goals > away_goals:
        home_row["points"] += 3
    elif away_goals > home_goals:
        away_row["points"] += 3
    else:
        home_row["points"] += 1
        away_row["points"] += 1


def replay_to_current_state(matches: list[dict]) -> dict:
    """Replay every finished match chronologically to arrive at each
    team's current tracked state — identical bookkeeping to
    build_dataset_apifootball.main(), just returning the final state
    instead of a training-row list.
    """
    state = new_state()
    for i, m in enumerate(matches, start=1):
        apply_match(state, m)
        if i % 2000 == 0:
            print(f"  ...replayed {i}/{len(matches)} historical matches", file=sys.stderr)
    return state


def fetch_upcoming_fixtures(days_ahead: int) -> list[dict]:
    current_year = datetime.date.today().year if datetime.date.today().month >= 7 else datetime.date.today().year - 1
    today = datetime.date.today()
    cutoff = today + datetime.timedelta(days=days_ahead)

    upcoming = []
    for code, info in LEAGUES.items():
        data = apifootball.get("/fixtures", {"league": info["id"], "season": current_year}, ttl_seconds=300)
        for m in data.get("response", []):
            if m["fixture"]["status"]["short"] not in ("NS", "TBD"):
                continue
            match_date = datetime.datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00")).date()
            if not (today <= match_date <= cutoff):
                continue
            upcoming.append({
                "fixture_id": m["fixture"]["id"],
                "date": m["fixture"]["date"],
                "competition": code,
                "season": current_year,
                "home": m["teams"]["home"]["name"],
                "away": m["teams"]["away"]["name"],
                "home_id": m["teams"]["home"]["id"],
                "away_id": m["teams"]["away"]["id"],
            })
    upcoming.sort(key=lambda m: m["date"])
    return upcoming


def attacking_form_for(state: dict, team_id: int, confirmed_lineup: list[tuple[int, str, str]] | None) -> float | None:
    """home/away_attacking_form for a live fixture -- prefers the REAL
    confirmed starting lineup when API-Football has already posted it
    (only true within ~1hr of kickoff), otherwise falls back to a "usual
    XI" proxy (this team's most-frequent starters over their last
    USUAL_XI_WINDOW tracked lineups, same logic build_lineup_features.py
    uses for lineup-change detection) since a live run is normally days
    ahead of kickoff and the real lineup doesn't exist yet.
    """
    if confirmed_lineup:
        attacker_ids = [pid for pid, pos, name in confirmed_lineup if pos in FORM_ATTACKING_POS]
    else:
        hist = state["team_lineup_history"][team_id]
        if len(hist) < USUAL_XI_MIN_HISTORY:
            return None
        freq: dict[int, int] = defaultdict(int)
        for past_xi in hist:
            for pid in past_xi:
                freq[pid] += 1
        usual_xi = set(sorted(freq, key=lambda p: -freq[p])[:11])
        attacker_ids = [pid for pid in usual_xi if state["player_positions"].get(pid) in FORM_ATTACKING_POS]

    if not attacker_ids:
        return None

    form_values = []
    for pid in attacker_ids:
        hist_g = state["player_goal_history"].get(pid)
        if hist_g and len(hist_g) >= MIN_STARTS_FOR_FORM:
            form_values.append(sum(hist_g) / len(hist_g))

    # Require full coverage, same rule build_player_form_features.py uses --
    # a missing player's contribution defaults to 0 rather than silently
    # biasing the sum down.
    if len(form_values) != len(attacker_ids):
        return None
    return round(sum(form_values), 4)


def build_feature_row(m: dict, state: dict) -> dict | None:
    home, away = m["home"], m["away"]
    competition = m["competition"]
    season_label = f"{competition}-{m['season']}"

    home_hist = state["team_history"][home]
    away_hist = state["team_history"][away]
    home_shot_hist = state["team_shot_history"][home]
    away_shot_hist = state["team_shot_history"][away]
    home_games_played = len(home_hist)
    away_games_played = len(away_hist)

    if home_games_played < 5 or away_games_played < 5:
        return None

    match_date = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
    home_pos, home_pts, home_gd = table_standing(state["table"], season_label, home)
    away_pos, away_pts, away_gd = table_standing(state["table"], season_label, away)
    home_rest = (match_date - state["team_last_played"][home]).days if home in state["team_last_played"] else None
    away_rest = (match_date - state["team_last_played"][away]).days if away in state["team_last_played"] else None
    pair_key = tuple(sorted([home, away]))
    h2h_goals = state["h2h_history"][pair_key]

    try:
        injuries = injury_count_for(m["fixture_id"])
    except apifootball.ApiFootballError:
        injuries = {}
    home_missing = injuries.get(m["home_id"], 0)
    away_missing = injuries.get(m["away_id"], 0)

    home_shots_last5 = rolling_avg(home_shot_hist, 5, "total_shots")
    away_shots_last5 = rolling_avg(away_shot_hist, 5, "total_shots")

    def safe_div(a, b):
        return None if a is None or b is None or b == 0 else a / b

    try:
        live_lineups = lineup_for(m["fixture_id"])
    except apifootball.ApiFootballError:
        live_lineups = {}
    home_attacking_form = attacking_form_for(state, m["home_id"], live_lineups.get(m["home_id"]))
    away_attacking_form = attacking_form_for(state, m["away_id"], live_lineups.get(m["away_id"]))

    return {
        "fixture_id": m["fixture_id"],
        "date": match_date.date().isoformat(),
        "competition": competition,
        "season": m["season"],
        "home_team": home,
        "away_team": away,
        "home_games_played": home_games_played,
        "away_games_played": away_games_played,
        "home_competition_games": state["team_competition_games"][(home, competition)],
        "away_competition_games": state["team_competition_games"][(away, competition)],
        "home_gf_last5": rolling_avg(home_hist, 5, "gf"),
        "home_ga_last5": rolling_avg(home_hist, 5, "ga"),
        "away_gf_last5": rolling_avg(away_hist, 5, "gf"),
        "away_ga_last5": rolling_avg(away_hist, 5, "ga"),
        "home_gf_last10": rolling_avg(home_hist, 10, "gf"),
        "home_ga_last10": rolling_avg(home_hist, 10, "ga"),
        "away_gf_last10": rolling_avg(away_hist, 10, "gf"),
        "away_ga_last10": rolling_avg(away_hist, 10, "ga"),
        "home_gf_season": season_avg(home_hist, season_label, "gf"),
        "home_ga_season": season_avg(home_hist, season_label, "ga"),
        "away_gf_season": season_avg(away_hist, season_label, "gf"),
        "away_ga_season": season_avg(away_hist, season_label, "ga"),
        "home_clean_sheet_pct_last5": clean_sheet_pct(home_hist, 5),
        "away_clean_sheet_pct_last5": clean_sheet_pct(away_hist, 5),
        "home_clean_sheet_pct_last10": clean_sheet_pct(home_hist, 10),
        "away_clean_sheet_pct_last10": clean_sheet_pct(away_hist, 10),
        "home_league_position": home_pos,
        "away_league_position": away_pos,
        "home_points": home_pts,
        "away_points": away_pts,
        "home_goal_diff": home_gd,
        "away_goal_diff": away_gd,
        "home_rest_days": home_rest,
        "away_rest_days": away_rest,
        "h2h_games": len(h2h_goals),
        "h2h_avg_goals": (sum(h2h_goals) / len(h2h_goals)) if h2h_goals else None,
        "home_shots_last5": home_shots_last5,
        "home_shots_on_goal_last5": rolling_avg(home_shot_hist, 5, "shots_on_goal"),
        "home_shots_inside_box_last5": rolling_avg(home_shot_hist, 5, "shots_inside_box"),
        "away_shots_last5": away_shots_last5,
        "away_shots_on_goal_last5": rolling_avg(away_shot_hist, 5, "shots_on_goal"),
        "away_shots_inside_box_last5": rolling_avg(away_shot_hist, 5, "shots_inside_box"),
        "home_conversion_rate_last5": safe_div(rolling_avg(home_hist, 5, "gf"), home_shots_last5),
        "away_conversion_rate_last5": safe_div(rolling_avg(away_hist, 5, "gf"), away_shots_last5),
        "home_missing_players": home_missing,
        "away_missing_players": away_missing,
        "home_xg_last5": rolling_avg_xg(state["xg_for_history"][home]),
        "away_xg_last5": rolling_avg_xg(state["xg_for_history"][away]),
        "home_xg_against_last5": rolling_avg_xg(state["xg_against_history"][home]),
        "away_xg_against_last5": rolling_avg_xg(state["xg_against_history"][away]),
        "home_finishing_last5": rolling_avg_xg(state["xg_finishing_history"][home]),
        "away_finishing_last5": rolling_avg_xg(state["xg_finishing_history"][away]),
        "home_attacking_form": home_attacking_form,
        "away_attacking_form": away_attacking_form,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=10, help="Look at fixtures in the next N days, default 10")
    args = parser.parse_args()

    print("Training the core model on the full historical dataset (team-goals-form swapped for player-form)...")
    historical = load_with_player_form()
    model_df = historical[CORE_CANDIDATES + ["over_2_5"]].dropna()
    print(f"  {len(model_df)} complete-case matches used for training")

    scaler = StandardScaler()
    X_train = scaler.fit_transform(model_df[CORE_CANDIDATES])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0,
    )
    model.fit(X_train, model_df["over_2_5"])

    print("Training the xG-augmented model on the real-xG-covered recent-era subset...")
    xg_historical = load_with_xg_and_player_form()
    xg_model_df = xg_historical[XG_CANDIDATES + ["over_2_5"]].dropna()
    print(f"  {len(xg_model_df)} complete-case matches used for training "
          f"(real xG only exists PL 2022-23+ / ELC 2023-24+ — validated on 3 rolling-origin "
          f"folds vs. the core model's 4, shorter track record)")

    xg_scaler = StandardScaler()
    X_xg_train = xg_scaler.fit_transform(xg_model_df[XG_CANDIDATES])
    xg_model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0,
    )
    xg_model.fit(X_xg_train, xg_model_df["over_2_5"])

    print("\nFetching upcoming fixtures...")
    try:
        upcoming = fetch_upcoming_fixtures(args.days)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not upcoming:
        print(f"No upcoming PL/Championship fixtures found in the next {args.days} days.")
        return 0

    print(f"Found {len(upcoming)} upcoming fixtures. Rebuilding current team state "
          f"(replaying historical matches, mostly cached — may take a minute)...")
    try:
        all_finished = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    state = replay_to_current_state(all_finished)

    print("\nScoring upcoming fixtures...")
    rows = []
    for m in upcoming:
        row = build_feature_row(m, state)
        if row is not None:
            rows.append(row)

    if not rows:
        print("No upcoming fixtures had enough team history to score.")
        return 0

    live_df = pd.DataFrame(rows)
    live_df = add_derived_features(live_df)
    live_df = add_xg_derived_features(live_df)
    live_df = add_player_form_derived_features(live_df)
    n_before_form = len(live_df)
    live_df = live_df.dropna(subset=CORE_CANDIDATES)
    if len(live_df) < n_before_form:
        print(f"  ({n_before_form - len(live_df)} fixture(s) dropped: missing a required feature, "
              f"often attacking-form coverage — a promoted team, or a starting attacker with <"
              f"{MIN_STARTS_FOR_FORM} tracked starts)")

    if live_df.empty:
        print("All upcoming fixtures were missing at least one required feature (likely shot-stat or form gaps).")
        return 0

    has_xg = live_df[XG_RAW_FEATURES + XG_DERIVED_FEATURES].notna().all(axis=1)

    live_df["pred_p_over_2_5"] = pd.NA
    live_df["model_used"] = ""

    core_rows = live_df.loc[~has_xg]
    if not core_rows.empty:
        X_core = scaler.transform(core_rows[CORE_CANDIDATES])
        live_df.loc[~has_xg, "pred_p_over_2_5"] = model.predict_proba(X_core)[:, 1]
        live_df.loc[~has_xg, "model_used"] = "core"

    xg_rows = live_df.loc[has_xg]
    if not xg_rows.empty:
        X_xg = xg_scaler.transform(xg_rows[XG_CANDIDATES])
        live_df.loc[has_xg, "pred_p_over_2_5"] = xg_model.predict_proba(X_xg)[:, 1]
        live_df.loc[has_xg, "model_used"] = "xG"

    live_df["pred_p_over_2_5"] = live_df["pred_p_over_2_5"].astype(float)
    live_df = live_df.sort_values("pred_p_over_2_5", ascending=False)

    print(f"\nUpcoming fixtures ranked by predicted P(over 2.5 goals):")
    print(f"  [xG]   = scored with the real-xG-augmented model (stronger, but only 3.5 years of track record)")
    print(f"  [core] = scored with the long-validated goals/shots model (fixture lacks real xG history — "
          f"e.g. a promoted team early in the season)")
    print("-" * 80)
    for _, r in live_df.iterrows():
        print(f"{r['date']}  [{r['competition']}] [{r['model_used']:<4s}]  {r['home_team']:<24s} vs {r['away_team']:<24s}  "
              f"P(over 2.5) = {r['pred_p_over_2_5']*100:.1f}%")

    top = live_df.iloc[0]
    print(f"\nTop pick: {top['home_team']} vs {top['away_team']} ({top['date']}) — "
          f"P(over 2.5) = {top['pred_p_over_2_5']*100:.1f}%  [{top['model_used']} model]")
    print("Model-derived estimate, not betting advice — see docs/apifootball-dataset-analysis.md "
          "and rolling_validation_xg.py for validation and caveats.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
