#!/usr/bin/env python3
"""Full-dataset coefficient check for the finish-gap mismatch proxy
(build_finish_gap_proxy_features.py). rolling_validation_finish_gap_proxy.py
already showed combined == baseline in every fold to 3-4 decimal places --
this confirms it on the complete dataset rather than skipping the step.

Usage:
    python3 check_finish_gap_proxy_full_dataset.py
"""

import warnings

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

from predict_upcoming import CORE_CANDIDATES
from rolling_validation_finish_gap_proxy import PROXY_FEATURES, load_data

warnings.filterwarnings("ignore")


def main() -> None:
    df = load_data()
    combined = list(dict.fromkeys(CORE_CANDIDATES + PROXY_FEATURES))
    model_df = df[combined + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X = scaler.fit_transform(model_df[combined])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X, model_df["over_2_5"])
    print(f"Trained on {len(model_df)} complete-case rows, C={model.C_[0]:.4f}")

    coef_map = dict(zip(combined, model.coef_[0]))
    for f in PROXY_FEATURES:
        c = coef_map[f]
        flag = "ZEROED" if abs(c) <= 1e-6 else "non-zero but check magnitude vs rest of model"
        print(f"\n{f}: coefficient = {c:+.6f}  ({flag})")

    print("\nTop 5 coefficients in the model for scale comparison:")
    for f, c in sorted(coef_map.items(), key=lambda x: -abs(x[1]))[:5]:
        print(f"  {f:<40s} {c:+.5f}")


if __name__ == "__main__":
    main()
