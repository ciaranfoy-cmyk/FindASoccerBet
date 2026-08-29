#!/usr/bin/env python3
"""Does a team's first-10-matchweek form predict the rest of that same season?

A narrower, more surgical test than the multi-season dataset: freeze each
team's goals-for/against/points from ONLY matchdays 1-10 of the 2025-26
season, then check whether that early-season snapshot has any bearing on
total goals in matchday 11 onward of THE SAME SEASON — no prior-season
history, no rolling window that crosses into the test period.

Usage:
    FOOTBALL_DATA_API_KEY=xxxx python3 analyze_early_season_form.py
"""

import sys
from collections import defaultdict

import pandas as pd
import statsmodels.api as sm
from scipy import stats
from sklearn.metrics import accuracy_score, roc_auc_score

import football_data

SEASON = 2025
EARLY_CUTOFF_MATCHDAY = 10
COMPETITIONS = ["PL", "ELC"]


def fetch_season_matches(competition: str) -> list[dict]:
    data = football_data.get(f"/competitions/{competition}/matches", {"season": SEASON}, ttl_seconds=None)
    matches = [m for m in data["matches"] if m["status"] == "FINISHED" and m["matchday"] is not None]
    matches.sort(key=lambda m: m["utcDate"])
    return matches


def build_early_form(matches: list[dict]) -> dict[str, dict]:
    """Each team's gf/ga/points/played from matchday <= EARLY_CUTOFF_MATCHDAY only."""
    form = defaultdict(lambda: {"gf": 0, "ga": 0, "points": 0, "played": 0})
    for m in matches:
        if m["matchday"] > EARLY_CUTOFF_MATCHDAY:
            continue
        home, away = m["homeTeam"]["name"], m["awayTeam"]["name"]
        hg, ag = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        form[home]["gf"] += hg
        form[home]["ga"] += ag
        form[home]["played"] += 1
        form[away]["gf"] += ag
        form[away]["ga"] += hg
        form[away]["played"] += 1
        if hg > ag:
            form[home]["points"] += 3
        elif ag > hg:
            form[away]["points"] += 3
        else:
            form[home]["points"] += 1
            form[away]["points"] += 1
    return dict(form)


def build_rows(competition: str) -> list[dict]:
    matches = fetch_season_matches(competition)
    early_form = build_early_form(matches)
    league_avg = sum(f["gf"] for f in early_form.values()) / sum(f["played"] for f in early_form.values())

    rows = []
    for m in matches:
        if m["matchday"] <= EARLY_CUTOFF_MATCHDAY:
            continue
        home, away = m["homeTeam"]["name"], m["awayTeam"]["name"]
        home_form, away_form = early_form.get(home), early_form.get(away)
        if not home_form or not away_form or not home_form["played"] or not away_form["played"]:
            continue

        hg, ag = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        home_gf_pg = home_form["gf"] / home_form["played"]
        home_ga_pg = home_form["ga"] / home_form["played"]
        away_gf_pg = away_form["gf"] / away_form["played"]
        away_ga_pg = away_form["ga"] / away_form["played"]

        rows.append({
            "date": m["utcDate"],
            "competition": competition,
            "matchday": m["matchday"],
            "home_team": home,
            "away_team": away,
            "home_gf_early": home_gf_pg,
            "home_ga_early": home_ga_pg,
            "away_gf_early": away_gf_pg,
            "away_ga_early": away_ga_pg,
            "home_points_pg_early": home_form["points"] / home_form["played"],
            "away_points_pg_early": away_form["points"] / away_form["played"],
            "naive_expected_total_early": home_gf_pg + away_ga_pg + away_gf_pg + home_ga_pg,
            "total_goals": hg + ag,
            "over_2_5": int((hg + ag) > 2.5),
        })
    return rows


def main() -> int:
    try:
        all_rows = []
        for comp in COMPETITIONS:
            rows = build_rows(comp)
            print(f"{comp}: {len(rows)} matches from matchday {EARLY_CUTOFF_MATCHDAY + 1} onward, "
                  f"using form frozen after matchday {EARLY_CUTOFF_MATCHDAY}")
            all_rows.extend(rows)
    except football_data.FootballDataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["is_PL"] = (df["competition"] == "PL").astype(int)
    df["points_gap_early"] = (df["home_points_pg_early"] - df["away_points_pg_early"]).abs()

    print(f"\nTotal test matches (matchday 11+, both competitions): {len(df)}")
    print(f"Base rate, over 2.5 in this window: {df['over_2_5'].mean()*100:.1f}%\n")

    print("=== Correlation: early-season form vs. later match outcomes ===")
    for label, sub in [("PL only", df[df["competition"] == "PL"]),
                        ("Championship only", df[df["competition"] == "ELC"]),
                        ("Combined", df)]:
        r1, p1 = stats.pearsonr(sub["naive_expected_total_early"], sub["total_goals"])
        r2, p2 = stats.pointbiserialr(sub["over_2_5"], sub["naive_expected_total_early"])
        print(f"{label:<20} n={len(sub):<5} corr w/ total_goals: r={r1:+.3f} p={p1:.4f}   "
              f"corr w/ over_2_5: r={r2:+.3f} p={p2:.4f}")

    # Chronological split within the matchday-11+ window itself
    split_idx = int(len(df) * 0.6)
    train, test = df.iloc[:split_idx], df.iloc[split_idx:]
    features = ["naive_expected_total_early", "points_gap_early", "is_PL"]

    X_train = sm.add_constant(train[features])
    model = sm.Logit(train["over_2_5"], X_train).fit(disp=0)
    X_test = sm.add_constant(test[features], has_constant="add")
    pred = model.predict(X_test)
    auc = roc_auc_score(test["over_2_5"], pred)
    acc = accuracy_score(test["over_2_5"], (pred >= 0.5).astype(int))
    baseline = max(test["over_2_5"].mean(), 1 - test["over_2_5"].mean())

    print(f"\n=== Logistic model (fit on earlier matchday-11+ games, tested on later ones) ===")
    print(f"Fit on: {len(train)} matches   Tested on: {len(test)} matches (never seen during fit)")
    print(model.summary())
    print(f"\nOut-of-sample: AUC={auc:.3f} (0.5=chance)  accuracy={acc*100:.1f}%  "
          f"vs. always-guess-majority baseline={baseline*100:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
