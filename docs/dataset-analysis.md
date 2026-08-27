# Dataset analysis: does anything here predict over/under 2.5 goals?

Built and tested properly this time: a real per-match dataset (not
season-standings snapshots), rolling pre-match features computed with no
lookahead, statistical significance testing corrected for the number of
features tried, and an honest out-of-sample evaluation. Reproducible with
`build_dataset.py` + `analyze_dataset.py`.

## The dataset

`build_dataset.py` pulls every finished Premier League + Championship
match available on the free tier (2023-24 through the in-progress
2026-27 season — 2,845 matches) and builds one row per match with
features computed from **only what happened before that match**, using
the actual match-by-match log rather than season-end standings (so a
promoted/relegated team's history carries over cleanly through the
division change). The first 5 games of each team's tracked history are
dropped since there isn't enough prior data to compute meaningful
rolling stats for them — leaves **2,705 matches**.

Per match: each team's rolling goals-for/against over their last 5 and
last 10 games, season-to-date average, games played in the current
competition (a proxy for top-flight experience), days of rest since
their last match, and head-to-head history between the two specific
teams. Output: `data/matches.csv`.

Base rate: **52.8%** of these matches finished over 2.5 goals.

## Univariate correlations

Tested 24 candidate features against both the raw total-goals count and
the over-2.5 binary outcome. With 24 tests running at once, testing each
at the usual p<0.05 bar risks ~1 false positive by chance alone — so the
real bar used is the Bonferroni-corrected one: **p < 0.00208**.

Features that survive it against `total_goals`:

| Feature | r | p |
|---|---|---|
| naive_expected_total_last5 (combined recent GF+GA) | +0.095 | <0.0001 |
| away_gf_last10 | +0.076 | 0.0001 |
| home_gf_last10 | +0.076 | 0.0001 |
| combined_gf_last5 | +0.074 | 0.0001 |
| away_competition_games (top-flight experience) | −0.065 | 0.0007 |
| away_gf_last5 | +0.062 | 0.0012 |
| home_gf_season | +0.062 | 0.0014 |

Against the binary `over_2_5` outcome specifically, only two survive the
correction: `naive_expected_total_last5` (p=0.0009) and `away_gf_last10`
(p=0.0015).

**Rest days were not significant for either target** (p=0.27–0.33),
consistent with the earlier single-season check — with 7x the data, the
signal still isn't there.

So: yes, real (if weak) relationships exist. Recent scoring form and
top-flight experience are genuinely, not-by-chance associated with how
many goals a match produces.

## Does any of this actually predict a specific game? No.

Statistical significance answers "is the true effect non-zero," not "is
this effect big enough to be useful." Tested that directly: fit a
logistic regression on the earliest 80% of matches (2023-09 through
2025-12), evaluate on the most recent 20% it never saw (2025-12 through
2026-08) — a genuine, honest out-of-sample check.

| Model | Out-of-sample AUC | Out-of-sample accuracy | Always-guess-majority baseline |
|---|---|---|---|
| 3-feature model (expected goals, experience, league) | 0.504 | 52.5% | 52.3% |
| Just "which league" (PL vs Championship) | 0.520 | 51.6% | 52.3% |
| Kitchen sink (every feature) | 0.499 | 51.2% | 52.3% |

**AUC 0.5 is a coin flip.** None of these clear it in any meaningful
way. The kitchen-sink model — throwing every available feature in at
once — scored *worse* than doing nothing, the textbook sign of fitting
noise rather than signal once you're combining several individually-weak
predictors.

## Conclusion

With box-score-level data alone (goals scored/conceded, rest days,
competition-experience, head-to-head record) — no actual xG, no
injury/lineup data, no betting-market prices — there isn't enough real
signal here to predict an individual match's over/under 2.5 outcome
better than the base rate. The correlations are real in the statistical
sense; they're just too weak, and too small relative to football's
inherent match-to-match variance, to move a prediction meaningfully.

This matches what's generally true in football analytics: genuine
predictive edges over a market-priced outcome like this typically need
either richer inputs (shot-quality xG, real injury/lineup news, market
odds themselves) or scale far beyond what a free-tier, box-score-only
API can provide. That's a real finding, not a dead end to paper over —
it tells you where the ceiling is for this data source specifically.

## Reproducing this

```bash
export FOOTBALL_DATA_API_KEY=your-key-here
pip install -r requirements.txt
python3 build_dataset.py     # writes data/matches.csv
python3 analyze_dataset.py   # prints the correlation table + model results above
```
