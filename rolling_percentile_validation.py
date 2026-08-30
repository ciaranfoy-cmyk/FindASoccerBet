#!/usr/bin/env python3
"""Test whether "top 5% by confidence" can actually be turned into a live,
no-lookahead decision rule — not just a retrospective batch statistic.

rolling_validation.py's top-5% number was computed by ranking an entire
test fold (up to ~1,900 games) all at once — which a real user could never
do in real time, since it requires already knowing the distribution of
predictions for games that haven't been played yet.

This instead walks through the test period one game at a time, in true
chronological order, and for each decision uses ONLY the trailing window
of predictions already made for earlier games (never later ones) to work
out where the live "95th percentile" bar currently sits. That adapts to
however the model's confidence range happens to be drifting era to era
(which is exactly what broke the fixed-threshold approaches), while
staying strictly causal.

Usage:
    python3 rolling_percentile_validation.py
    python3 rolling_percentile_validation.py --window 500 --percentile 95
"""

import argparse
import warnings
from collections import deque
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import ALL_CANDIDATES, load

warnings.filterwarnings("ignore")

N_FOLDS = 5


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

    out = test[["date", "over_2_5"]].copy()
    out["pred_p"] = pred_prob
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window", type=int, default=500, help="Trailing window size (# of past predictions)")
    parser.add_argument("--percentile", type=float, default=95.0, help="Percentile of the trailing window to use as the live bar")
    parser.add_argument("--warmup", type=int, default=200, help="Skip betting until the trailing window has at least this many predictions")
    args = parser.parse_args()

    df = load()
    model_df = df[ALL_CANDIDATES + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    # Same expanding-window retraining schedule as rolling_validation.py —
    # produces one strictly-causal predicted probability per test-fold game.
    all_predictions = []
    for fold in range(1, N_FOLDS):
        train_end = boundaries[fold]
        test_start, test_end = boundaries[fold], boundaries[fold + 1]
        train, test = model_df.iloc[:train_end], model_df.iloc[test_start:test_end]
        preds = fit_and_predict(train, test, ALL_CANDIDATES)
        all_predictions.append(preds)
        print(f"Fold {fold}: trained on {len(train)}, scored {len(preds)} games "
              f"({preds['date'].min().date()} to {preds['date'].max().date()})")

    stream = pd.concat(all_predictions, ignore_index=True).sort_values("date").reset_index(drop=True)
    print(f"\nWalking {len(stream)} games in chronological order, "
          f"window={args.window}, live bar = trailing p{args.percentile:.0f}, warmup={args.warmup}\n")

    trailing = deque(maxlen=args.window)
    bets = wins = 0
    skipped_warmup = skipped_below_bar = 0
    bar_history = []

    for _, row in stream.iterrows():
        if len(trailing) >= args.warmup:
            bar = pd.Series(trailing).quantile(args.percentile / 100.0)
            bar_history.append(bar)
            if row["pred_p"] >= bar:
                bets += 1
                wins += int(row["over_2_5"])
            else:
                skipped_below_bar += 1
        else:
            skipped_warmup += 1
        trailing.append(row["pred_p"])

    baseline = model_df["over_2_5"].mean()
    print(f"Games in warmup (no decision made): {skipped_warmup}")
    print(f"Games below the live bar (skipped): {skipped_below_bar}")
    print(f"Bets placed: {bets}   Wins: {wins}")
    if bar_history:
        bh = pd.Series(bar_history)
        print(f"Live bar over time: min={bh.min()*100:.1f}%  median={bh.median()*100:.1f}%  max={bh.max()*100:.1f}%")

    if bets:
        hit_rate = wins / bets
        expected = bets * baseline
        std = sqrt(bets * baseline * (1 - baseline))
        z = (wins - expected) / std
        p = 0.5 * (1 - erf(z / sqrt(2)))
        print(f"\nHit rate: {wins}/{bets} = {hit_rate*100:.1f}%  vs baseline {baseline*100:.1f}%   z={z:.2f} p={p:.4f}")
        print(f"(compare: the batch/retrospective top-5%-per-fold number was 239/380 = 62.9%)")
    else:
        print("\nNo bets ever placed — window/warmup/percentile settings left nothing eligible.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
