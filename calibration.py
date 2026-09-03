#!/usr/bin/env python3
"""Recalibrates the model's raw probability output. The model is
overconfident in a consistent way (see calibration_full_history.py -- a
"72%" prediction has historically meant ~60%), so this fits a
translation curve from raw probability to true probability, separately
for the core and xG models since they're miscalibrated by different
amounts (the xG model's overconfidence is much worse at the top end:
-18.7pp vs the core model's -4.8pp in the top decile).

Isotonic regression (fully flexible, monotonic curve) was tried first
and REJECTED: on a single chronological 50/50 holdout split it made
Brier score WORSE, not better (core 0.2465->0.2474, xG 0.2494->0.2512)
-- with only ~1,300 out-of-fold xG points to fit from, isotonic's extra
flexibility overfits noise rather than capturing a real, stable pattern.
A more robust 5-fold cross-validated check confirmed this and showed
Platt scaling (a simple 2-parameter logistic curve, much less prone to
overfitting with limited data) generalizes better for both models:

    core: raw 0.2478 -> isotonic 0.2485 (worse) -> Platt 0.2475
    xG:   raw 0.2523 -> isotonic 0.2481 (better) -> Platt 0.2475 (best)

So this fits Platt scaling (logistic regression on the raw probability
as a single feature), not isotonic. The improvement is real but modest
(Brier moves by 0.0003-0.0048) -- a small bias correction, not a
transformation of the model. Both scaling methods are monotonic, so
neither changes the RANKING of predictions within a model's own stream
-- doesn't change which games top-N/week or rolling-percentile select,
only the stated probability, which is what matters for computing real
edge against a market price (Kalshi) instead of a raw, inflated number.

Note the 5-fold CV check above used shuffle=False KFold blocks, which
for the early blocks trains on chronologically LATER data than it
tests on -- a weaker standard than the walk-forward, no-lookahead
discipline used everywhere else in this project. That's a reasonable
tradeoff here because the question being tested is narrower ("is this
correction curve a stable property of the model's bias, not noise"),
not "would this have been knowable live" -- but it's still worth
flagging rather than quietly borrowing the project's usual rigor.

Usage:
    python3 calibration.py             # evaluate honestly, then fit + save
"""

import os
import pickle
import warnings

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import KFold

from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from build_xg_weighted_features import load_weighted_xg

warnings.filterwarnings("ignore")

CALIBRATOR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "calibrators.pkl")


class PlattCalibrator:
    """Wraps a 1-feature LogisticRegression so it has the same .predict(x)
    -> calibrated-probability interface calibration.py's callers expect."""

    def __init__(self):
        self.model = LogisticRegression()

    def fit(self, raw_p, y):
        self.model.fit(np.asarray(raw_p).reshape(-1, 1), np.asarray(y))
        return self

    def predict(self, raw_p):
        return self.model.predict_proba(np.asarray(raw_p).reshape(-1, 1))[:, 1]


def build_hybrid_streams() -> tuple[pd.DataFrame, pd.DataFrame]:
    # Deferred imports: predict_upcoming.py imports load_calibrators/apply_calibration
    # from this module, and backtest_season_rolling_percentile.py imports
    # CORE_CANDIDATES/XG_CANDIDATES from predict_upcoming -- importing either
    # at module level here would be circular.
    from backtest_season_rolling_percentile import N_FOLDS_CORE, N_FOLDS_XG, build_stream
    from predict_upcoming import CORE_CANDIDATES, XG_CANDIDATES

    df = load_with_xg_player_form_and_shots_venue()
    df = load_weighted_xg(df)
    core_stream = build_stream(df, CORE_CANDIDATES, N_FOLDS_CORE, "core").sort_values("date").reset_index(drop=True)
    xg_stream = build_stream(df, XG_CANDIDATES, N_FOLDS_XG, "xG").sort_values("date").reset_index(drop=True)
    return core_stream, xg_stream


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def evaluate_cv(stream: pd.DataFrame, label: str) -> None:
    """5-fold check (see module docstring for why this is a slightly
    weaker standard than the project's usual walk-forward discipline):
    reports raw vs. Platt-calibrated Brier score, averaged across folds
    where each fold's calibrator is fit only on the other folds."""
    p = stream["pred_p"].to_numpy()
    y = stream["over_2_5"].to_numpy(dtype=float)
    kf = KFold(n_splits=5, shuffle=False)

    raw_briers, platt_briers = [], []
    for train_idx, test_idx in kf.split(p):
        p_tr, y_tr, p_te, y_te = p[train_idx], y[train_idx], p[test_idx], y[test_idx]
        raw_briers.append(_brier(p_te, y_te))
        cal = PlattCalibrator().fit(p_tr, y_tr)
        platt_briers.append(_brier(cal.predict(p_te), y_te))

    print(f"  {label}: raw Brier={np.mean(raw_briers):.4f}  "
          f"Platt-calibrated Brier={np.mean(platt_briers):.4f}  (n={len(stream)}, 5-fold)")


def fit_and_save() -> None:
    core_stream, xg_stream = build_hybrid_streams()

    print("Cross-validated evaluation (Platt scaling, chosen over isotonic -- see module docstring):")
    evaluate_cv(core_stream, "core")
    evaluate_cv(xg_stream, "xG")

    print("\nRefitting on the full stream (all data) for the saved calibrator...")
    core_cal = PlattCalibrator().fit(core_stream["pred_p"], core_stream["over_2_5"])
    xg_cal = PlattCalibrator().fit(xg_stream["pred_p"], xg_stream["over_2_5"])

    # Pickle only the plain sklearn LogisticRegression (.model), never the
    # PlattCalibrator wrapper itself -- a class defined in a script run as
    # __main__ pickles under the name "__main__.PlattCalibrator", which
    # can't be found when unpickled from a different script that imports
    # this module normally. Plain sklearn objects don't have this problem.
    os.makedirs(os.path.dirname(CALIBRATOR_PATH), exist_ok=True)
    with open(CALIBRATOR_PATH, "wb") as f:
        pickle.dump({"core": core_cal.model, "xg": xg_cal.model}, f)
    print(f"Saved calibrators to {CALIBRATOR_PATH}")

    # A few example points so the correction is visible, not just trusted.
    print("\nExample corrections (core model):")
    for raw_p in [0.30, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90]:
        print(f"  raw {raw_p*100:.0f}% -> calibrated {core_cal.predict([raw_p])[0]*100:.1f}%")
    print("Example corrections (xG model):")
    for raw_p in [0.30, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90]:
        print(f"  raw {raw_p*100:.0f}% -> calibrated {xg_cal.predict([raw_p])[0]*100:.1f}%")


def load_calibrators() -> dict:
    """Returns {"core": sklearn LogisticRegression, "xg": sklearn LogisticRegression}."""
    with open(CALIBRATOR_PATH, "rb") as f:
        return pickle.load(f)


def _platt_predict(sk_model, raw_p: np.ndarray) -> np.ndarray:
    return sk_model.predict_proba(np.asarray(raw_p).reshape(-1, 1))[:, 1]


def apply_calibration(pred_p: pd.Series, model_used: pd.Series, calibrators: dict) -> pd.Series:
    """pred_p: raw model probabilities. model_used: parallel 'core'/'xG'
    labels. calibrators: as returned by load_calibrators(). Returns
    calibrated probabilities, same index."""
    out = pred_p.astype(float).copy()
    core_mask = model_used == "core"
    xg_mask = model_used == "xG"
    if core_mask.any():
        out.loc[core_mask] = _platt_predict(calibrators["core"], pred_p.loc[core_mask].to_numpy())
    if xg_mask.any():
        out.loc[xg_mask] = _platt_predict(calibrators["xg"], pred_p.loc[xg_mask].to_numpy())
    return out


if __name__ == "__main__":
    fit_and_save()
