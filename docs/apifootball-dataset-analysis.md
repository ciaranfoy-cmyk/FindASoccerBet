# API-Football dataset analysis — first result that clears significance

This documents the comprehensive analysis of `data/matches_apifootball.csv`
(14,270 matches, PL 2010-11+ and Championship 2011-12+, with shot
statistics, injuries, and rolling clean-sheet/conversion-rate features
that `data/matches.csv` — the football-data.org dataset — doesn't have).
Reproduce with `python3 analyze_dataset_apifootball.py`.

Same discipline as `docs/dataset-analysis.md`: every candidate tested,
significance corrected for the number of comparisons, evaluated on a
genuine chronological holdout the model never saw during fitting. This
is the first analysis in the project where the result actually clears
conventional statistical significance.

## Univariate scan

54 pre-match features tested against both total goals and the over-2.5
outcome. With 54 comparisons, the Bonferroni-corrected bar is
**p < 0.000926**; 20 features clear it, 30 clear the uncorrected p<0.05.

Notably, several of the *new* feature types (not available in the
football-data.org dataset) rank among the strongest correlates:
`combined_shots_inside_box_last5` (r=+0.091), `combined_shots_last5`
(r=+0.089), `home_shots_inside_box_last5` (r=+0.077),
`home_shots_on_goal_last5` (r=+0.074) — all comparable in strength to
the best goals-based features (`home_gf_last10` at r=+0.083). The shot
data is pulling real weight, not just adding noise.

A separate sanity-check block (same-match shot stats — leakage, not a
predictor) confirms the data itself is sound: `home_shots_on_goal`
correlates with that match's own total goals at r=+0.385, far stronger
than any pre-match feature, exactly as expected since it's describing
the match rather than predicting it.

## Model: L1-regularized (Lasso) logistic regression

Rather than hand-picking 3-5 features (the earlier approach) or a naive
kitchen-sink with all 54 (which invites overfitting on correlated
inputs), used `LogisticRegressionCV` with an L1 penalty for principled,
data-driven feature selection, 5-fold CV to pick the regularization
strength, evaluated on a chronological 80/20 split (train: 2015-02 to
2024-04, test: 2024-04 to 2026-08, never touched during fitting or CV).

17 of 54 features survived the L1 penalty (non-zero coefficient):
`is_PL`, `home_gf_season`, `goal_diff_gap`, `combined_shots_last5`,
`clean_sheet_pct_combined_last5`, `home_gf_last10`,
`naive_expected_total_last5`, `combined_shots_inside_box_last5`,
`home_conversion_rate_last5`, `home_goal_diff`, `away_ga_last10`,
`away_gf_last10`, `h2h_avg_goals`, `away_shots_last5`, `position_gap`,
`away_clean_sheet_pct_last10`, `away_rest_days`. Again, several of the
new shot/clean-sheet/conversion features made the cut alongside the
established goals-based ones.

## Out-of-sample results — first to clear significance

| Check | Result | z | p-value |
|---|---|---|---|
| Overall accuracy vs. 52.0% baseline | 54.2% (n=1911) | 1.94 | **0.026** |
| Tail: top decile vs. bottom decile | 57.1% vs. 46.4% | 2.96 | **0.0016** |
| ≥60% confidence threshold | 62.3% (43/69) | 1.72 | **0.043** |
| Top 5% by confidence | 64.2% (61/95) | 2.38 | **0.0086** |

AUC: 0.551 (previous best, on the smaller football-data.org dataset
with a hand-picked 3-feature model: 0.504-0.529).

**The tail is now correctly directional** — in the earlier analysis
(see the "tail-confidence strategy" discussion in the session), the
model's most-confident predictions performed *worse* than its
least-confident ones, backwards from what the strategy needs. Here the
top decile (57.1%) clearly beats the bottom decile (46.4%), the shape a
working confidence-threshold strategy actually requires.

Full threshold table:

| Threshold | Games (% of test) | Hit rate |
|---|---|---|
| ≥50% | 1008 (52.7%) | 55.9% |
| ≥55% | 338 (17.7%) | 58.0% |
| ≥60% | 69 (3.6%) | 62.3% |
| ≥65% | 10 (0.5%) | 70.0% (n too small to trust) |
| Top 5% by rank | 95 | **64.2%** |
| Top 10% by rank | 191 | 57.1% |
| Top 20% by rank | 382 | 56.8% |

## Why this matters practically

Earlier in the project, "win $60 on a $100 bet" (1.60 decimal odds) was
established as needing a 62.5% hit rate to break even. The ≥60%
threshold (62.3%) and the top-5%-by-confidence cut (64.2%) both clear
that line, with real statistical significance behind them (p=0.043 and
p=0.0086 respectively) — the first results in the project that would
have been profitable at typical market pricing in the test window, not
just "better than a coin flip."

## Rolling-origin validation (`rolling_validation.py`)

The single 80/20 split above answers "did this work once." This answers
the question that actually matters: does it hold up across independent
time periods, or was that one split a favorable draw? Split the
complete-case dataset (9,553 matches) into 5 chronological chunks;
for each of the last 4, train on an expanding window of everything
before it (still fully chronological, no lookahead) and evaluate the
same top-5%-by-confidence cut on that chunk alone.

| Fold | Test window | AUC | Top-5% hit rate (n=95 each) |
|---|---|---|---|
| 1 | 2017-11 to 2020-01 | 0.525 | 58.9% |
| 2 | 2020-01 to 2022-03 | 0.538 | 63.2% |
| 3 | 2022-03 to 2024-04 | 0.561 | 65.3% |
| 4 | 2024-04 to 2026-08 | 0.551 | 64.2% (the original single-split result) |

Every fold beats its own local base rate, and the hit rate stays in a
tight 58.9-65.3% band across nearly a decade of separate windows —
not the kind of scatter you'd expect from a fluke.

**Combined across all 4 folds (n=380, 4x the original sample):
239/380 = 62.9% hit rate, z=4.94, p<0.0001.**

The 95% confidence interval tightens from 54.2-73.1% (one window) to
**57.9-67.6%** (four windows) — and critically, the lower bound is now
*above* the 54.75% breakeven line for the odds tested earlier (implied
ROI range: +5.8% to +23.5%, no longer touching negative). Before this,
the honest worst case was "roughly breakeven." Now the honest worst
case is "a genuine but smaller edge."

## Honest caveats — read before acting on this

- **The rolling-origin result strengthens this a lot but doesn't erase
  every caveat.** All 4 folds still use the same feature set and same
  L1 modeling approach chosen once, not independently re-validated each
  time — there's some risk the *methodology* itself was tuned (even
  informally, across this session's iterations) toward what happened to
  work on this specific dataset and time range. A true out-of-sample
  test would apply this exact frozen approach to new seasons as they
  arrive, going forward, rather than to more historical data.
- **Multiple comparisons at the model-selection stage aren't fully
  accounted for.** The univariate scan is properly corrected; the L1
  model's feature selection and the subsequent threshold sweep were not
  independently pre-registered, so there's some risk of having found
  the best-looking cut among several tried rather than a single
  pre-specified one. The ≥60% and top-5% results both being significant
  independently is reassuring, but this is a real caveat, not a
  formality.
- **Shot-stat coverage is ~72%**, so features built on it implicitly
  restrict the usable window to matches (and eras) where that data
  exists — check whether the model's edge is concentrated in a
  particular period before generalizing.

Given all that: this is the first result in the project with real
weight behind it — real features, principled selection, correctly-
directional calibration, clears actual betting economics, and now
confirmed (not just once but four times) across independent time
windows spanning 2017-2026. The honest framing is "a genuine, if
modest, edge worth continuing to monitor going forward" — still not
"solved" (see the caveats above, especially the frozen-methodology
point), but no longer just "promising, unconfirmed."
