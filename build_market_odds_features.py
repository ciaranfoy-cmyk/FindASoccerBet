#!/usr/bin/env python3
"""Historical market-odds feature builder -- writes data/market_odds_features.csv
(fixture_id, mkt_over25_prob) for training the "odds-augmented" model
tier. See market_odds_features.py's docstring for why Bet365, the kill
switch, and the scope restriction (PL/LALIGA/SERIEA only -- the
leagues this feature was actually validated on).

Re-fetches thestatsapi's own match list to get its match_id per
fixture, matches to our own fixture_id by (competition, date, fuzzy
team names), then pulls each match's Bet365 total_goals odds. Every
response is cached on disk (market_odds_features.CACHE_DIR) via the
same _get() used for live fetches, so re-running this after the first
time costs nothing.

Usage:
    STATSAPI_KEY=xxxx python3 build_market_odds_features.py
"""

import csv
import os
import sys

import pandas as pd

from market_odds_features import COVERED_COMPETITIONS, StatsApiError, _devig_over, _get, _normalize

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market_odds_features.csv")
CUTOFF_DATE = "2019-08-01"
BOOKMAKER = "Bet365"


def fetch_all_matches(competition_id: str) -> list[dict]:
    all_matches, page = [], 1
    while True:
        d = _get("/football/matches", {"competition_id": competition_id, "status": "finished", "per_page": 100, "page": page})
        batch = d["data"]
        if not batch:
            break
        all_matches.extend(batch)
        if page >= d["meta"]["total_pages"]:
            break
        page += 1
    return all_matches


def main() -> int:
    hist = pd.read_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "matches_apifootball.csv"))
    rows = []

    for comp_code, comp_id in COVERED_COMPETITIONS.items():
        our_matches = hist[hist["competition"] == comp_code]
        our_by_date = {}
        for _, r in our_matches.iterrows():
            our_by_date.setdefault(str(r["date"])[:10], []).append(r)

        print(f"\n=== {comp_code} ===", file=sys.stderr)
        try:
            statsapi_matches = fetch_all_matches(comp_id)
        except StatsApiError as exc:
            print(f"  Could not fetch match list: {exc}", file=sys.stderr)
            continue
        statsapi_matches = [m for m in statsapi_matches if m["utc_date"][:10] >= CUTOFF_DATE and m.get("odds_available")]
        print(f"  {len(statsapi_matches)} matches from {CUTOFF_DATE} onward with odds_available=True", file=sys.stderr)

        matched, with_odds = 0, 0
        for i, m in enumerate(statsapi_matches, 1):
            date = m["utc_date"][:10]
            candidates = our_by_date.get(date, [])
            m_home, m_away = _normalize(m["home_team"]["name"]), _normalize(m["away_team"]["name"])
            our_row = None
            for r in candidates:
                r_home, r_away = _normalize(r["home_team"]), _normalize(r["away_team"])
                if (m_home in r_home or r_home in m_home) and (m_away in r_away or r_away in m_away):
                    our_row = r
                    break
            if our_row is None:
                continue
            matched += 1

            try:
                odds = _get(f"/football/matches/{m['id']}/odds")
            except StatsApiError as exc:
                print(f"    [{i}/{len(statsapi_matches)}] odds fetch failed -- {exc}", file=sys.stderr)
                continue
            bk = next((b for b in odds["data"]["bookmakers"] if b["bookmaker"] == BOOKMAKER), None)
            if bk is None:
                continue
            tg = bk["markets"].get("total_goals", {})
            line = tg.get("2.5", {})
            prob = _devig_over(line.get("over", {}).get("last_seen"), line.get("under", {}).get("last_seen"))
            if prob is None:
                continue
            with_odds += 1
            rows.append({"fixture_id": our_row["fixture_id"], "mkt_over25_prob": prob})

            if i % 200 == 0:
                print(f"    ...{i}/{len(statsapi_matches)}", file=sys.stderr)

        print(f"  matched {matched}/{len(statsapi_matches)} to our own fixtures, {with_odds} with a usable Bet365 2.5 line", file=sys.stderr)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["fixture_id", "mkt_over25_prob"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
