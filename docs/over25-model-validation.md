# Over/under 2.5 goals model — validation notes

This documents how the non-venue over/under-2.5 model in `analyze_matchweek.py`
was validated, so the thresholds and the promotion penalty aren't just
asserted in a docstring.

## The model

Ignores home/away splits — just each team's overall goals-for/against rate
vs. the opponent's, normalized by the competition's overall average
goals/team/game:

```
lambda_home = avg * (home_gf_per_game / avg) * (away_ga_per_game / avg)
lambda_away = avg * (away_gf_per_game / avg) * (home_ga_per_game / avg)
```

`P(over 2.5)` comes from summing the Poisson joint probability mass over
all `(h, a)` with `h + a <= 2` and subtracting from 1.

## Backtest: full 2025-26 Premier League season (38 gameweeks, 380 games)

Model built from 2023-24 + 2024-25 standings only (no lookahead into the
season being predicted).

**"Top 5 highest-confidence picks per week" (the original framing):**

| Version | Games covered | Top-5 hit rate |
|---|---|---|
| PL-only stats (no Championship merge, no penalty) | 306/380 | 112/190 = 58.9% |
| + cross-division merge (Championship history for promoted/relegated teams) | 380/380 | 115/190 = 60.5% |
| + promotion penalty on top | 380/380 | 116/190 = 61.1% |

Season-wide baseline (how often games actually went over 2.5, regardless
of any picking): 209/380 = 55.0%.

So picking the model's weekly top 5 beats picking randomly by only ~5-6
points — a real but modest edge, and it forces exactly 5 picks a week
whether or not the model actually has an opinion worth acting on.

## Reframing: how many games can we *confidently* call?

Rather than forcing 5 picks/week, look at hit rate by confidence threshold
across all 380 games (with the cross-division + promotion-penalty model):

| Threshold | Games qualifying | Hit rate | Edge vs. 55.0% baseline |
|---|---|---|---|
| >= 50% | 272 (7.2/wk) | 58.1% | +3.1 pts |
| >= 55% | 222 (5.8/wk) | 58.6% | +3.6 pts |
| >= 60% | 124 (3.3/wk) | 59.7% | +4.7 pts |
| >= 65% | 54 (1.4/wk) | **63.0%** | +8.0 pts |
| >= 70% | 4 (0.1/wk) | 75.0% | +20.0 pts (n=4, too small to trust) |

At "win $60 on a $100 bet" odds (1.60 decimal), the breakeven win rate is
62.5%. The >=65% bucket (63.0%, n=54) is the first one that clears it —
barely, and on one season's sample — while >=60% (59.7%) still doesn't.
That's the basis for the tool's default `--over25-threshold 0.60`
(conservative middle ground) with a printed note pointing at 0.65 as
where the real edge starts. The >=70% bucket is too small a sample (4
games) to draw anything from.

## Calibration check

Predicted-probability buckets vs. actual over-2.5 rate, same season:

| Predicted bucket | Actual rate |
|---|---|
| 50-60% | 61.4% (n=70) |
| 60-70% | 59.5% (n=116) |
| 70-80% | 75.0% (n=4) |

Reasonably well calibrated — not miscalibrated, just working with real
match-to-match variance. Bucketing by raw predicted total xG instead of
probability shows the same thing more starkly, confirming higher summed
xG does track a higher actual over-2.5 rate:

| Predicted total xG | Actual over-2.5 rate |
|---|---|
| 1.5-2.0 | 25.0% (n=4) |
| 2.0-2.5 | 40.9% (n=66) |
| 2.5-3.0 | 56.8% (n=132) |
| 3.0-3.5 | 59.0% (n=144) |
| 3.5-4.0 | 61.8% (n=34) |

## Promotion penalty derivation

Comparing each 2025-26 promoted team's actual PL scoring/conceding rate
against their pre-season (2023-24 + 2024-25, Championship-blended)
baseline:

| Team | Actual GF/g | Baseline GF/g | Actual GA/g | Baseline GA/g |
|---|---|---|---|---|
| Burnley | 1.00 | 1.31 (−0.31) | 1.97 | 1.12 (**+0.85**) |
| Leeds United | 1.29 | 1.91 (−0.62) | 1.47 | 0.79 (**+0.68**) |
| Sunderland | 1.11 | 1.20 (−0.09) | 1.26 | 1.07 (+0.20) |

All three moved the same direction on both sides: scored less than their
Championship-derived baseline suggested, and conceded meaningfully more.
Averaging the three gives `PROMOTION_PENALTY = {"attack": -0.34,
"defense": +0.58}` goals/game, applied in `analysis.py` to any team with
zero top-flight games in the analysis window.

**Caveat: n=3.** This is one season's worth of promoted teams. It's
directionally consistent across all three and matches the intuitive
story (a step up in competition level hits defense harder than attack),
but it should be revisited once more promoted-team seasons are available
to fold in — right now it's a reasonable prior, not a precise estimate.

## What didn't make it in

- **Fixture congestion / rest days.** Tested short-rest (<=4 days) vs.
  normal-rest games using PL-only fixture dates: 56.3% over-2.5 (n=103)
  vs. 54.7% (n=267) — a ~1.6 point gap, too small and too likely
  confounded to trust. The real problem is data coverage: rest days
  computed from PL fixtures alone miss a team's actual midweek games in
  the Champions League, Europa League, FA Cup, etc. (all of which
  football-data.org does return per-team via `/teams/{id}/matches`, just
  not through the two domestic-league endpoints this tool otherwise
  uses). A trustworthy version would need to pull each team's full
  cross-competition match history to compute true rest days — doable,
  but a meaningfully bigger scope than the current tool, so it's left
  as a documented gap rather than a half-measured feature.

- **"Striker in form."** A real signal in principle, but needs
  per-player match-by-match goal logs (`/persons/{id}/matches`), a squad
  mapping to know which players are relevant to which fixture, and a
  definition of "hot streak" worth encoding — a substantially bigger
  feature than anything else in this tool. Not attempted here; a good
  candidate for a dedicated follow-up rather than bolting on quickly.
