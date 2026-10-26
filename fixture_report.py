"""Why is the model predicting this fixture? -- a repeatable one-fixture
report in a fixed format, for any fixture in the upcoming window.

Sections (always the same, in this order):
  1. Verdict      -- model %, percentile among the trailing 500
                     out-of-fold predictions, bar, pool hit rate, live
                     asks + edge per venue (Kalshi, Polymarket US,
                     international Polymarket), pick / not a pick.
  2. Drivers      -- every non-zero term of the model's log-odds sum
                     (coefficient x standardized feature), biggest first,
                     with the raw value and the training average, plus
                     what the probability would be with the top driver
                     at an average value.
  3. Team context -- each side's last 5 matches: score, own xG, xG
                     against.
  4. Trust checks -- automatic flags: thin team history / promoted side,
                     features far outside the training range, model vs
                     market gap, early-season.

It reuses explain_picks.py's own pipeline (same models, bars, replayed
state and prices), so the numbers match the picks table exactly.

Usage:
    python3 fixture_report.py Elversberg            # match on a team name
    python3 fixture_report.py --fixture-id 1575211
    python3 fixture_report.py Marseille --days 10 --out /mnt/project-files/reports/
"""

import argparse
import datetime
import math
import os
import sys
import time
import warnings

import pandas as pd

import explain_picks as ep
import model_cache
from build_dataset_apifootball import fetch_all_fixtures
from build_xg_features import xg_stats_for
from forward_test_log import OVER_DISALLOWED_COMPETITIONS, kalshi_fee
from polymarket_prices import polymarket_fee

warnings.filterwarnings("ignore")

TRAILING_KEY = "trailing_oof_over_500"
THIN_HISTORY_GAMES = 20      # fewer games than this in our data = thin history
EXTREME_Z = 3.0              # a driver this many SDs from the training mean is extrapolation
MARKET_GAP_PP = 5.0          # model vs devigged market gap worth calling out


def _capture_live_df(days: int) -> pd.DataFrame:
    captured = {}

    def grab(live_df, *a, **k):
        captured["df"] = live_df.copy()

    ep.print_picks_table = grab
    argv = sys.argv
    sys.argv = ["explain_picks.py", "--table", "--days", str(days)]
    try:
        ep.main()
    finally:
        sys.argv = argv
    return captured["df"]


def _trailing_predictions() -> pd.Series:
    cached = model_cache.load(TRAILING_KEY)
    if cached is not None:
        return cached
    print("Building the trailing out-of-fold stream for percentiles (one-off, ~10 min)...")
    from forward_test_log import _calibrated_stream
    s = _calibrated_stream()
    s = s[~s["competition"].isin(OVER_DISALLOWED_COMPETITIONS)]
    trailing = s["pred_p"].dropna().tail(500).reset_index(drop=True)
    model_cache.save(TRAILING_KEY, trailing)
    return trailing


def _poly_us_ask(home: str, away: str) -> tuple[float | None, str | None]:
    """Live best ask of the Over 2.5 market on Polymarket US, found via
    its search (US event slugs use their own abbreviations)."""
    try:
        import polymarket_trading as pus
    except Exception:
        return None, None
    try:
        res = pus.search(home, limit=20)
    except Exception:
        return None, None
    away_key = ep._normalize(away).split()[0] if ep._normalize(away) else ""
    for e in res.get("events", []):
        if e.get("closed") or away_key not in ep._normalize(e.get("title", "")):
            continue
        slug = f"tsc-{e['slug']}-2pt5"
        # The gateway rate-limits quick calls (429 code 1015); only a 404
        # means the event has no Over 2.5 market.
        for wait in (2, 8, 20):
            time.sleep(wait)
            try:
                bbo = pus.get_market_bbo(slug)["marketData"]
                ask = bbo.get("bestAsk")
                return (float(ask["value"]) if ask else None), slug
            except Exception as exc:
                if "404" in str(exc):
                    return None, f"{e['slug']} (no Over 2.5 market)"
        return None, f"{slug} (lookup rate-limited, recheck)"
    return None, None


def _last5(team_id: int, before: str, all_finished: list[dict]) -> list[dict]:
    games = [m for m in all_finished if team_id in (m["home_id"], m["away_id"]) and m["date"] < before][-5:]
    out = []
    for m in reversed(games):
        home = m["home_id"] == team_id
        try:
            xg = xg_stats_for(m["fixture_id"])
        except Exception:
            xg = {}
        opp_id = m["away_id"] if home else m["home_id"]
        out.append({
            "date": m["date"][:10], "comp": m["competition"],
            "opp": m["away"] if home else m["home"], "venue": "H" if home else "A",
            "gf": m["home_goals"] if home else m["away_goals"],
            "ga": m["away_goals"] if home else m["home_goals"],
            "xg": xg.get(team_id, {}).get("xg"), "xga": xg.get(opp_id, {}).get("xg"),
        })
    return out


def _fmt(v, d=2):
    return "--" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{d}f}"


def build_report(r: pd.Series, bundle, trailing: pd.Series, all_finished: list[dict]) -> str:
    (bar, _ub, early_bar, _eub, pool_over, pool_over_early, _pu,
     model, scaler, xg_model, xg_scaler, *_rest) = bundle
    use_xg = r["model_used"].startswith("xG")
    feats = ep.XG_CANDIDATES if use_xg else ep.CORE_CANDIDATES
    mdl, scl = (xg_model, xg_scaler) if use_xg else (model, scaler)
    home, away = r["home_team"], r["away_team"]

    p = float(r["calibrated_p"])
    pctl = (trailing < p).mean() * 100
    eff_bar = early_bar if r["is_early_season"] else bar
    pool = pool_over_early[0] if r["is_early_season"] else pool_over[0]
    clears = p >= eff_bar and r["competition"] not in OVER_DISALLOWED_COMPETITIONS
    price_p = pool if clears else p

    bet365 = ep._fetch_bet365_fair(r["competition"], home, away, r["date"][:10])
    pus_ask, pus_slug = _poly_us_ask(home, away)
    venues = [
        ("Kalshi", r["kalshi_yes_ask"], kalshi_fee),
        ("Polymarket US", pus_ask, polymarket_fee),
        ("Polymarket (intl, reference)", r["poly_yes_ask"], polymarket_fee),
    ]
    venue_rows, best = [], None
    for name, ask, fee in venues:
        if ask is None or pd.isna(ask):
            venue_rows.append(f"| {name} | not listed | -- |")
            continue
        edge = (price_p - float(ask) - fee(float(ask))) * 100
        venue_rows.append(f"| {name} | ${float(ask):.2f} | {edge:+.1f} |")
        if "intl" not in name and (best is None or edge > best[1]):
            best = (name, edge, float(ask))

    if not clears:
        decision = f"Not a pick: {p*100:.1f}% is below the {eff_bar*100:.1f}% bar, so its edge is not validated."
    elif best is None:
        decision = "Clears the bar, but there is no tradable market on Kalshi or Polymarket US yet."
    elif best[1] <= 0:
        decision = f"Clears the bar, but no positive edge: the best ask (${best[2]:.2f} on {best[0]}) is too high."
    else:
        decision = f"Pick: clears the bar with {best[1]:+.1f} pts of edge on {best[0]} at ${best[2]:.2f}."

    # -- drivers: every non-zero term of the log-odds sum --
    x = scl.transform(r[feats].to_frame().T.astype(float))[0]
    terms = []
    for i, (f, c) in enumerate(zip(feats, mdl.coef_[0])):
        if abs(c * x[i]) >= 0.001:
            terms.append((f, c * x[i], float(r[f]), scl.mean_[i], x[i]))
    terms.sort(key=lambda t: -abs(t[1]))
    logit = mdl.intercept_[0] + sum(t[1] for t in terms)
    top = terms[0]
    p_raw_without_top = 1 / (1 + math.exp(-(logit - top[1])))

    lines = [
        f"# Why the model likes {home} vs {away} (Over 2.5)",
        "",
        f"{r['competition']} · {r['date'][:16].replace('T', ' ')} UTC · fixture {r['fixture_id']} · report run {datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC",
        "",
        "## 1. Verdict",
        "",
        decision,
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Model | {r['model_used']} |",
        f"| Model probability (calibrated) | {p*100:.1f}% |",
        f"| Percentile vs trailing 500 predictions | p{int(pctl)} |",
        f"| Bar ({'early-season' if r['is_early_season'] else 'p95'}) | {eff_bar*100:.1f}% |",
        f"| Probability used for edge | {price_p*100:.1f}% ({'validated pool hit rate' if clears else 'own estimate, not validated'}) |",
        f"| Bet365 devigged | {_fmt(bet365*100 if bet365 else None, 1)}% |",
        f"| Market fair (Kalshi / intl Poly, devigged) | {_fmt(r['kalshi_fair_p']*100 if pd.notna(r['kalshi_fair_p']) else None, 1)}% / {_fmt(r['poly_fair_p']*100 if pd.notna(r['poly_fair_p']) else None, 1)}% |",
        "",
        "| Venue | Over 2.5 ask | Edge (pts, after fee) |",
        "|---|---|---|",
        *venue_rows,
        "",
        f"Polymarket US market: `{pus_slug or 'not found'}`.",
        "",
        "## 2. What drives the number",
        "",
        f"The model adds up the terms below (coefficient x how far the feature sits from its training average), "
        f"plus an intercept of {mdl.intercept_[0]:+.3f}, to get log-odds of {logit:+.3f}, which is {1/(1+math.exp(-logit))*100:.1f}% before calibration. "
        f"The biggest term is **{ep._describe(top[0], home, away)}**. With that one feature at its average, "
        f"the model would say about {p_raw_without_top*100:.1f}% (raw).",
        "",
        "| # | Feature | Value | Training avg | SDs from avg | Log-odds push |",
        "|---|---|---|---|---|---|",
    ]
    for i, (f, contrib, val, mean, z) in enumerate(terms, 1):
        lines.append(f"| {i} | {ep._describe(f, home, away)} (`{f}`) | {val:.3f} | {mean:.3f} | {z:+.2f} | {contrib:+.3f} |")
    toward = sum(t[1] for t in terms if t[1] > 0)
    away_sum = sum(t[1] for t in terms if t[1] < 0)
    lines += ["", f"Terms pushing toward Over sum to {toward:+.3f}; terms pushing toward Under sum to {away_sum:+.3f}.", ""]

    # -- team context --
    lines += ["## 3. Team context (last 5 matches in our data)", ""]
    ids = {}
    for m in all_finished:
        ids[m["home"]] = m["home_id"]
        ids[m["away"]] = m["away_id"]
    for team in (home, away):
        if team not in ids:
            continue
        games = _last5(ids[team], r["date"], all_finished)
        lines += [f"**{team}**", "", "| Date | Comp | Opponent | H/A | Score | xG | xG against | Total goals |", "|---|---|---|---|---|---|---|---|"]
        for g in games:
            lines.append(f"| {g['date']} | {g['comp']} | {g['opp']} | {g['venue']} | {g['gf']}-{g['ga']} | {_fmt(g['xg'])} | {_fmt(g['xga'])} | {g['gf'] + g['ga']} |")
        overs = sum(1 for g in games if g["gf"] + g["ga"] >= 3)
        lines += ["", f"{overs} of {len(games)} went Over 2.5.", ""]

    # -- trust checks --
    flags = []
    for side, team in (("home", home), ("away", away)):
        gp = r.get(f"{side}_games_played")
        if gp is not None and pd.notna(gp) and gp < THIN_HISTORY_GAMES:
            flags.append(f"{team} has only {int(gp)} games in our data (likely promoted), so its form numbers rest on a small sample.")
    for f, contrib, val, mean, z in terms[:5]:
        if abs(z) >= EXTREME_Z:
            flags.append(f"`{f}` is {z:+.1f} SDs from the training average, near the edge of what the model has seen.")
    market = r["kalshi_fair_p"] if pd.notna(r["kalshi_fair_p"]) else r["poly_fair_p"]
    if pd.notna(market) and (p - market) * 100 >= MARKET_GAP_PP:
        flags.append(f"The model is {(p - market)*100:.1f} pts above the devigged market ({market*100:.1f}%): the market disagrees.")
    if r["is_early_season"]:
        flags.append("Early season: the stricter early-season bar applies.")
    share = abs(top[1]) / max(1e-9, sum(abs(t[1]) for t in terms))
    if share >= 0.5:
        flags.append(f"One feature carries {share*100:.0f}% of the model's total push, so the call rests mostly on it.")
    lines += ["## 4. Trust checks", ""]
    lines += [f"- {f}" for f in flags] if flags else ["- No flags."]
    lines += ["", f"Pool hit rate behind the bar: {pool*100:.1f}% Over (n={(pool_over_early if r['is_early_season'] else pool_over)[1]}).", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("team", nargs="?", help="part of either team's name")
    ap.add_argument("--fixture-id", type=int)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=None, help="directory to also write the report .md into")
    args = ap.parse_args()
    if not args.team and not args.fixture_id:
        ap.error("give a team name or --fixture-id")

    live = _capture_live_df(args.days)
    if args.fixture_id:
        hit = live[live["fixture_id"] == args.fixture_id]
    else:
        key = args.team.lower()
        hit = live[live["home_team"].str.lower().str.contains(key) | live["away_team"].str.lower().str.contains(key)]
    if hit.empty:
        print("No matching fixture in the window.")
        return 1
    r = hit.iloc[0]
    bundle = model_cache.load("explain_picks_bundle")
    report = build_report(r, bundle, _trailing_predictions(), fetch_all_fixtures(None))
    print("\n" + report)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        name = f"{r['date'][:10]}_{r['home_team']}_v_{r['away_team']}".replace(" ", "_")
        path = os.path.join(args.out, f"{name}.md")
        with open(path, "w") as f:
            f.write(report)
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
