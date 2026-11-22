#!/usr/bin/env python3
"""Is the p90-p95 band worth betting? The live picks only trust fixtures
at or above the rolling p95 bar, whose pool hit rate (compute_pool_hit_rate)
prices every pick's edge. Fixtures between the rolling p90 and p95 bars
have never had a validated hit rate of their own; their table edge falls
back to the fixture's own calibrated probability.

Replays the exact live selection rule (forward_test_log.compute_pool_hit_rate:
trailing 500 out-of-fold predictions, warmup 200, causal -- a fixture's
bars come only from predictions before it) and reports, per band:
hit rate with a 95% Wilson interval, mean stated probability, and -- where
Bet365's devigged Over 2.5 price exists (data/market_odds_features.csv)
-- the market's mean implied probability and the flat-$1 ROI at that
fair price (no vig, no exchange fee: an upper bound).

Usage:
    python3 check_p90_band.py
"""

import math
import os
import warnings
from collections import deque

import pandas as pd

import model_cache
from forward_test_log import OVER_DISALLOWED_COMPETITIONS, _calibrated_stream

warnings.filterwarnings("ignore")

WINDOW, WARMUP = 500, 200
BANDS = [("p90-p95", 90.0, 95.0), ("p95+ (live pool)", 95.0, None), ("p90+ combined", 90.0, None)]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


def tag_bars(stream: pd.DataFrame) -> pd.DataFrame:
    stream = stream.dropna(subset=["pred_p"]).sort_values("date").reset_index(drop=True)
    trailing: deque = deque(maxlen=WINDOW)
    p90s, p95s = [], []
    for v in stream["pred_p"]:
        if len(trailing) >= WARMUP:
            s = pd.Series(trailing)
            p90s.append(s.quantile(0.90))
            p95s.append(s.quantile(0.95))
        else:
            p90s.append(float("nan"))
            p95s.append(float("nan"))
        trailing.append(v)
    stream["bar90"], stream["bar95"] = p90s, p95s
    return stream.dropna(subset=["bar90"])


def main() -> int:
    stream = model_cache.load("calibrated_stream")
    if stream is None:
        print("Building the out-of-fold calibrated stream (~10 min, cached afterwards)...")
        stream = _calibrated_stream()
        model_cache.save("calibrated_stream", stream)
    stream = stream[~stream["competition"].isin(OVER_DISALLOWED_COMPETITIONS)]
    stream = tag_bars(stream)

    odds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market_odds_features.csv")
    odds = pd.read_csv(odds_path) if os.path.exists(odds_path) else pd.DataFrame(columns=["fixture_id", "mkt_over25_prob"])
    stream = stream.merge(odds, on="fixture_id", how="left")

    print(f"Out-of-fold stream: {len(stream)} fixtures after warmup, {stream['date'].min():%Y-%m-%d} to {stream['date'].max():%Y-%m-%d}\n")
    rows = []
    for label, lo, hi in BANDS:
        lo_bar = stream["bar90"] if lo == 90.0 else stream["bar95"]
        sel = stream[stream["pred_p"] >= lo_bar]
        if hi is not None:
            sel = sel[sel["pred_p"] < sel["bar95"]]
        k, n = int(sel["over_2_5"].sum()), len(sel)
        ci = wilson(k, n)
        priced = sel.dropna(subset=["mkt_over25_prob"])
        roi = ((priced["over_2_5"] / priced["mkt_over25_prob"]) - 1).mean() if len(priced) else float("nan")
        rows.append({
            "band": label, "n": n, "hit_rate": k / n if n else float("nan"),
            "ci_low": ci[0], "ci_high": ci[1], "mean_model_p": sel["pred_p"].mean(),
            "n_priced": len(priced), "hit_rate_priced": priced["over_2_5"].mean(),
            "mean_market_p": priced["mkt_over25_prob"].mean(), "roi_at_fair": roi,
        })
        print(f"{label:18s} n={n:4d}  hit {k/n*100:5.1f}% (95% CI {ci[0]*100:.1f}-{ci[1]*100:.1f})  "
              f"model says {sel['pred_p'].mean()*100:.1f}%  | with Bet365 price: n={len(priced)}, "
              f"hit {priced['over_2_5'].mean()*100:.1f}% vs market {priced['mkt_over25_prob'].mean()*100:.1f}%, "
              f"ROI at fair {roi*100:+.1f}%")

    band = stream[(stream["pred_p"] >= stream["bar90"]) & (stream["pred_p"] < stream["bar95"])].copy()
    band["season_label"] = band["date"].dt.year
    print("\np90-p95 band by calendar year:")
    for yr, g in band.groupby("season_label"):
        print(f"  {yr}: n={len(g):4d}  hit {g['over_2_5'].mean()*100:5.1f}%  model {g['pred_p'].mean()*100:.1f}%")
    print("\np90-p95 band by competition:")
    for comp, g in band.groupby("competition"):
        print(f"  {comp:13s} n={len(g):4d}  hit {g['over_2_5'].mean()*100:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
