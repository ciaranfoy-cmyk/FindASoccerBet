#!/usr/bin/env python3
"""How often do games actually go over 2.5 when there's a big quality
mismatch, empirically? Built in direct response to the observation that
current-season "gap" features (goal_diff_gap, position_gap) are thin
early in a season -- every team's current-season stats are small-sample
early on, not just a newly-promoted team's -- while avg_finish_gap
(build_league_finish_features.py) is built from full PRIOR seasons, so
it already carries a real, undiluted signal for a team like Coventry
(avg_finish=30.0, the maximum penalty, meaning no real top-flight
pedigree at all) from the very first match of a new season.

avg_finish_gap itself was tested directly as a linear feature already
(rolling_validation_league_finish.py / check inside
check_xg_weighted_full_dataset.py) and got essentially zeroed out. This
takes a different, non-parametric approach instead of feeding the raw
gap value into the logistic regression: bucket ABS(avg_finish_gap) into
mismatch-size buckets, and for each bucket compute the EMPIRICAL over-2.5
rate among all prior matches (any two teams, expanding window, strict
no-lookahead) that fell in that bucket. A match's proxy feature is then
"historically, games with a mismatch this size went over X% of the
time" -- letting the data speak for the actual over/under relationship
at each mismatch size directly, rather than assuming linearity.

Same no-lookahead discipline as everything else: a match's proxy value
only ever reflects bucket outcomes from strictly earlier matches.

Usage:
    python3 build_finish_gap_proxy_features.py
"""

import csv
import os
from collections import defaultdict

import pandas as pd

FINISH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "league_finish_features.csv")
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "finish_gap_proxy_features.csv")

BUCKET_WIDTH = 5.0  # abs(avg_finish_gap) bucketed into width-5 bands: 0-5, 5-10, ...
MIN_BUCKET_GAMES = 30  # don't trust a bucket's rate until it has real sample size


def bucket_for(abs_gap: float) -> int:
    return int(abs_gap // BUCKET_WIDTH)


def main() -> int:
    finish_df = pd.read_csv(FINISH_PATH)

    # Need actual goals to know over_2_5 -- build_dataset_apifootball's
    # fetch_all_fixtures gives us that directly, same source used to
    # build league_finish_features.csv in the first place, so fixture_id
    # alignment is exact.
    from build_dataset_apifootball import fetch_all_fixtures
    matches = fetch_all_fixtures(None)
    goals_by_fixture = {m["fixture_id"]: (m["home_goals"], m["away_goals"]) for m in matches}

    finish_df = finish_df.sort_values("date").reset_index(drop=True)
    print(f"Processing {len(finish_df)} matches (pure replay, no new API calls)")

    bucket_stats: dict[int, dict] = defaultdict(lambda: {"games": 0, "overs": 0})

    rows = []
    for i, r in enumerate(finish_df.itertuples(), start=1):
        fixture_id = r.fixture_id
        gap = r.avg_finish_gap

        proxy_pct = None
        bucket = None
        if pd.notna(gap):
            abs_gap = abs(gap)
            bucket = bucket_for(abs_gap)
            stats = bucket_stats[bucket]
            if stats["games"] >= MIN_BUCKET_GAMES:
                proxy_pct = stats["overs"] / stats["games"]

        rows.append({
            "fixture_id": fixture_id, "date": r.date,
            "avg_finish_gap": gap,
            "finish_gap_bucket": bucket,
            "finish_gap_mismatch_over_pct": proxy_pct,
        })

        # Update state AFTER computing this match's pre-match feature.
        goals = goals_by_fixture.get(fixture_id)
        if goals is not None and bucket is not None:
            home_goals, away_goals = goals
            is_over = (home_goals + away_goals) > 2.5
            bucket_stats[bucket]["games"] += 1
            bucket_stats[bucket]["overs"] += int(is_over)

        if i % 5000 == 0:
            print(f"  ...{i}/{len(finish_df)}")

    print("\nFinal bucket over-rates (all history, for reference):")
    for b in sorted(bucket_stats):
        s = bucket_stats[b]
        lo, hi = b * BUCKET_WIDTH, (b + 1) * BUCKET_WIDTH
        rate = s["overs"] / s["games"] if s["games"] else float("nan")
        print(f"  gap [{lo:.0f}-{hi:.0f}): {s['games']:>6d} games, {rate*100:.1f}% over")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
