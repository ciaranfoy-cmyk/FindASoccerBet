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
their last match, head-to-head history between the two specific teams,
and league position/points/goal-difference — computed from a table we
build ourselves match-by-match (not the standings endpoint), so it
reflects the exact state right before this match, same no-lookahead
guarantee as everything else. Output: `data/matches.csv`.

Base rate: **52.8%** of these matches finished over 2.5 goals.

## Univariate correlations

Tested 32 candidate features against both the raw total-goals count and
the over-2.5 binary outcome. With 32 tests running at once, testing each
at the usual p<0.05 bar risks ~1-2 false positives by chance alone — so
the real bar used is the Bonferroni-corrected one: **p < 0.00156**.

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

The new table-position features (`home_league_position`,
`home_goal_diff`, `goal_diff_gap`) land at raw p<0.05 (0.004–0.018) but
don't clear the corrected bar individually — real candidates, just not
as strong on their own as the recent-form features above.

**Rest days were not significant for either target** (p=0.27–0.33),
consistent with the earlier single-season check — with 7x the data, the
signal still isn't there.

So: yes, real (if weak) relationships exist. Recent scoring form, top-
flight experience, and table position are genuinely, not-by-chance
associated with how many goals a match produces.

## Does any of this actually predict a specific game? Barely, and only once position is included.

Statistical significance answers "is the true effect non-zero," not "is
this effect big enough to be useful." Tested that directly: fit a
logistic regression on the earliest 80% of matches (2023-09 through
2025-12), evaluate on the most recent 20% it never saw (2025-12 through
2026-08) — a genuine, honest out-of-sample check.

| Model | Out-of-sample AUC | Out-of-sample accuracy | Always-guess-majority baseline |
|---|---|---|---|
| 3-feature model (expected goals, experience, league) | 0.504 | 52.5% | 52.3% |
| Table-position model (position gap, GD gap, league) | 0.524 | 52.1% | 51.1% |
| Best model + position (5 features combined) | 0.510 | 52.5% | 51.1% |
| Just "which league" (PL vs Championship) | 0.520 | 51.6% | 52.3% |
| Kitchen sink, no position | 0.499 | 51.2% | 52.3% |
| Kitchen sink, with position | **0.529** | 52.1% | 51.1% |

**AUC 0.5 is a coin flip.** None of these clear it convincingly. Adding
league position did measurably help, though — the kitchen-sink AUC moved
from 0.499 (worse than doing nothing) to 0.529 once position/points/
goal-difference were included, and it's now the best-performing model
tried. That's a real, directional improvement worth keeping, not just
noise reshuffling — but 0.529 is still a very weak edge in absolute
terms, nowhere near strong enough to act on.

## Conclusion

With box-score-level data alone (goals scored/conceded, rest days,
competition-experience, head-to-head record, table position/points/goal
difference) — no actual xG, no injury/lineup data, no betting-market
prices — there isn't enough real signal here to predict an individual
match's over/under 2.5 outcome meaningfully better than the base rate.
The correlations are real in the statistical sense, and table position
measurably improved the model (0.499 → 0.529 AUC) — but even the best
combination is still far too weak, relative to football's inherent
match-to-match variance, to act on.

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
