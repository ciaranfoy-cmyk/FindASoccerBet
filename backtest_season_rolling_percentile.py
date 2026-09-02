#!/usr/bin/env python3
"""Run the validated, causal rolling-percentile live rule (see
rolling_percentile_validation.py) across one full season of PL +
Championship fixtures and report exactly what it would have bet.

Hybrid model: builds two expanding-window prediction streams — the core
model (5 folds, full history) and the xG-augmented model (4 folds, real
xG only exists PL 2022-23+ / ELC 2023-24+) — then for each game prefers
the xG prediction when available (its own out-of-sample fold), falling
back to the core prediction otherwise. This mirrors predict_upcoming.py's
live hybrid logic. No per-week retraining needed either way — the
trailing-window bar is computed purely from the stream of past
predictions, which the fold structure already produces in full
chronological, no-lookahead order.

Usage:
    python3 backtest_season_rolling_percentile.py --season 2025
    python3 backtest_season_rolling_percentile.py --season 2025 --window 750
"""

import argparse
import warnings
from collections import deque
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from build_dataset_apifootball import LEAGUES
from predict_upcoming import CORE_CANDIDATES, XG_CANDIDATES

warnings.filterwarnings("ignore")

N_FOLDS_CORE = 5
N_FOLDS_XG = 4
STAKE = 100.0


def fit_and_predict(train: pd.DataFrame, test: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    scaler = StandardScaler()
    X_train = scaler.fit_transform(train[features])
    X_test = scaler.transform(test[features])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X_train, train["over_2_5"])
    pred_prob = model.predict_proba(X_test)[:, 1]
    out = test[["fixture_id", "date", "over_2_5", "home_team", "away_team", "competition"]].copy()
    out["pred_p"] = pred_prob
    return out


def build_stream(df: pd.DataFrame, features: list[str], n_folds: int, label: str) -> pd.DataFrame:
    cols = ["fixture_id", "date", "home_team", "away_team", "competition", "over_2_5"] + features
    model_df = df[cols].dropna().reset_index(drop=True)
    fold_size = len(model_df) // n_folds
    boundaries = [i * fold_size for i in range(n_folds + 1)]
    boundaries[-1] = len(model_df)

    all_predictions = []
    for fold in range(1, n_folds):
        train_end = boundaries[fold]
        test_start, test_end = boundaries[fold], boundaries[fold + 1]
        train, test = model_df.iloc[:train_end], model_df.iloc[test_start:test_end]
        preds = fit_and_predict(train, test, features)
        all_predictions.append(preds)
        print(f"  [{label}] Fold {fold}: trained on {len(train)}, scored {len(preds)} games")

    if not all_predictions:
        return pd.DataFrame(columns=cols[:-len(features)] + ["pred_p"])
    return pd.concat(all_predictions, ignore_index=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int, default=2025, help="Season start-year, e.g. 2025 = 2025-26")
    parser.add_argument("--window", type=int, default=500, help="Trailing window size (# of past predictions)")
    parser.add_argument("--percentile", type=float, default=95.0, help="Percentile of the trailing window used as the live bar")
    parser.add_argument("--warmup", type=int, default=200, help="Games needed in the trailing window before betting starts")
    parser.add_argument("--profit-on-win", type=float, default=90.0,
                         help="Profit per $100 stake on a win (default 90 = decimal odds 1.90). "
                              "Real over-2.5 odds vary a lot by fixture -- a heavy favorite can price "
                              "as low as ~$35-40. This is a flat assumption, not fixture-specific odds.")
    args = parser.parse_args()
    profit_on_win = args.profit_on_win

    # Determine the season's actual date span from real fixtures.
    season_dates = []
    for code, info in LEAGUES.items():
        data = apifootball.get("/fixtures", {"league": info["id"], "season": args.season})
        for m in data["response"]:
            if m["fixture"]["status"]["short"] == "FT":
                season_dates.append(pd.Timestamp(m["fixture"]["date"]).tz_localize(None))
    window_start, window_end = min(season_dates), max(season_dates)
    print(f"Season {args.season}-{args.season+1-2000}: {window_start.date()} to {window_end.date()}\n")

    df = load_with_xg_player_form_and_shots_venue()

    core_stream = build_stream(df, CORE_CANDIDATES, N_FOLDS_CORE, "core")
    xg_stream = build_stream(df, XG_CANDIDATES, N_FOLDS_XG, "xG")

    core_stream = core_stream.rename(columns={"pred_p": "pred_p_core"})
    xg_small = xg_stream[["fixture_id", "pred_p"]].rename(columns={"pred_p": "pred_p_xg"})
    merged = core_stream.merge(xg_small, on="fixture_id", how="left")
    merged["pred_p"] = merged["pred_p_xg"].combine_first(merged["pred_p_core"])
    merged["model_used"] = merged["pred_p_xg"].notna().map({True: "xG", False: "core"})
    stream = merged.sort_values("date").reset_index(drop=True)

    print(f"\nWalking {len(stream)} games chronologically (hybrid xG/core), "
          f"window={args.window}, live bar=trailing p{args.percentile:.0f}, warmup={args.warmup}\n")

    trailing = deque(maxlen=args.window)
    season_games_seen = 0
    season_games_scored = 0  # past warmup, i.e. a real bar existed
    bets = []
    bankroll = 0.0

    for _, row in stream.iterrows():
        in_season = window_start <= row["date"] <= window_end
        if len(trailing) >= args.warmup:
            bar = pd.Series(trailing).quantile(args.percentile / 100.0)
            if in_season:
                season_games_scored += 1
                if row["pred_p"] >= bar:
                    win = bool(row["over_2_5"])
                    bankroll += profit_on_win if win else -STAKE
                    bets.append({
                        "date": row["date"].date(), "competition": row["competition"],
                        "home": row["home_team"], "away": row["away_team"],
                        "pred_p": row["pred_p"], "bar": bar, "model_used": row["model_used"],
                        "win": win, "bankroll": bankroll,
                    })
        if in_season:
            season_games_seen += 1
        trailing.append(row["pred_p"])

    print("=" * 100)
    print(f"Bets placed during season {args.season}-{args.season+1-2000} (hybrid xG/core):")
    print("=" * 100)
    for b in bets:
        outcome = "WIN " + f"+${profit_on_win:.0f}" if b["win"] else "LOSS " + f"-${STAKE:.0f}"
        print(f"{b['date']}  [{b['competition']}] [{b['model_used']:<4s}] {b['home']:<20s} vs {b['away']:<20s}  "
              f"pred={b['pred_p']*100:5.1f}%  bar={b['bar']*100:5.1f}%  {outcome:<12s}  bankroll={b['bankroll']:+.0f}")

    n_bets = len(bets)
    wins = sum(1 for b in bets if b["win"])
    n_xg_bets = sum(1 for b in bets if b["model_used"] == "xG")
    print("\n" + "-" * 100)
    print(f"Season games in window: {season_games_seen}   Games with a live bar available: {season_games_scored}")
    print(f"Bets placed: {n_bets} ({n_xg_bets} via xG model, {n_bets - n_xg_bets} via core)   Wins: {wins}   Losses: {n_bets - wins}")
    if n_bets:
        total_staked = n_bets * STAKE
        print(f"Hit rate: {wins}/{n_bets} = {wins/n_bets*100:.1f}%")
        print(f"Total staked: ${total_staked:.0f}")
        print(f"Total P&L: ${bankroll:+.0f}")
        print(f"ROI: {bankroll/total_staked*100:+.1f}%")
        baseline = stream["over_2_5"].mean()
        expected = n_bets * baseline
        std = sqrt(n_bets * baseline * (1 - baseline))
        if std > 0:
            z = (wins - expected) / std
            p = 0.5 * (1 - erf(z / sqrt(2)))
            print(f"vs. overall baseline {baseline*100:.1f}%: z={z:.2f} p={p:.4f} (n this small — indicative only)")
    else:
        print("No bets placed this season.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
