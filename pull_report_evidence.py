#!/usr/bin/env python3
"""One-shot batch pull of the real-world evidence behind every card in
the weekend report: for both teams in each fixture, their last 5 games
specifically at the relevant venue (home team's home games / away team's
away games) with shots + xG, their season-to-date actual results, and
the actual attacking players driving each side's attacking-form number
(resolved to real names via the squad endpoint). Written as one batch
pass reusing a single state replay and cached team squads, instead of
13 separate rounds of API calls.

Usage:
    APIFOOTBALL_KEY=xxxx python3 pull_report_evidence.py
"""
import datetime
import json
from collections import defaultdict

import apifootball
from build_dataset_apifootball import fetch_all_fixtures, shot_stats_for
from build_xg_features import xg_stats_for
from build_player_form_features import ATTACKING_POS
from predict_upcoming import new_state, apply_match

FIXTURES = [
    ("LIGUE1", 1552749, "Lens", 116, "Lorient", 97, "2026-09-05T15:15:00+00:00"),
    ("MLS", 1490438, "Philadelphia Union", 1599, "CF Montreal", 1614, "2026-09-05T23:30:00+00:00"),
    ("BUNDESLIGA", 1575151, "Bayer Leverkusen", 168, "Union Berlin", 182, "2026-09-05T13:30:00+00:00"),
    ("EREDIVISIE", 1552155, "Utrecht", 207, "GO Ahead Eagles", 410, "2026-09-05T16:45:00+00:00"),
    ("BUNDESLIGA", 1575152, "Eintracht Frankfurt", 169, "FC Augsburg", 170, "2026-09-06T15:30:00+00:00"),
    ("PL", 1557395, "Newcastle", 34, "Bournemouth", 35, "2026-09-05T11:30:00+00:00"),
    ("LIGUE1", 1552750, "Lyon", 80, "Auxerre", 108, "2026-09-04T17:00:00+00:00"),
    ("MLS", 1490450, "Portland Timbers", 1617, "Minnesota United FC", 1612, "2026-09-06T02:30:00+00:00"),
    ("BUNDESLIGA", 1575156, "FC Schalke 04", 174, "Bayern München", 157, "2026-09-05T16:30:00+00:00"),
    ("MLS", 1490437, "Inter Miami", 9568, "Atlanta United FC", 1608, "2026-09-05T23:30:00+00:00"),
    ("EREDIVISIE", 1552154, "NEC Nijmegen", 413, "Feyenoord", 209, "2026-09-05T14:30:00+00:00"),
    ("EREDIVISIE", 1552136, "NEC Nijmegen", 413, "Excelsior", 196, "2026-09-08T16:45:00+00:00"),
    ("MLS", 1490439, "FC Cincinnati", 2242, "DC United", 1615, "2026-09-05T23:30:00+00:00"),
]

_squad_cache: dict[int, dict[int, str]] = {}


def squad_names(team_id: int) -> dict[int, str]:
    if team_id in _squad_cache:
        return _squad_cache[team_id]
    try:
        data = apifootball.get("/players/squads", {"team": team_id}, ttl_seconds=None)
        resp = data.get("response", [])
        names = {p["id"]: p["name"] for p in resp[0]["players"]} if resp else {}
    except apifootball.ApiFootballError:
        names = {}
    _squad_cache[team_id] = names
    return names


def venue_games(prior: list, team_name: str, venue: str, before: datetime.datetime, n: int = 5) -> list:
    games = [m for m in prior if m[venue] == team_name and
             datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00")) < before]
    out = []
    for m in games[-n:]:
        try:
            shots = shot_stats_for(m["fixture_id"])
            team_id = m["home_id"] if venue == "home" else m["away_id"]
            s = shots.get(team_id, {})
        except apifootball.ApiFootballError:
            s = {}
        try:
            xg = xg_stats_for(m["fixture_id"])
            team_id = m["home_id"] if venue == "home" else m["away_id"]
            xg_val = xg.get(team_id, {}).get("xg")
        except apifootball.ApiFootballError:
            xg_val = None
        out.append({
            "date": m["date"][:10],
            "home": m["home"], "away": m["away"],
            "home_goals": m["home_goals"], "away_goals": m["away_goals"],
            "shots": s.get("total_shots"), "on_target": s.get("shots_on_goal"), "inside_box": s.get("shots_inside_box"),
            "xg": xg_val,
        })
    return out


def season_games(prior: list, team_name: str, competition: str, season: int, before: datetime.datetime) -> list:
    games = [m for m in prior if (m["home"] == team_name or m["away"] == team_name)
             and m["competition"] == competition and m["season"] == season
             and datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00")) < before]
    out = []
    for m in games:
        venue = "home" if m["home"] == team_name else "away"
        gf, ga = (m["home_goals"], m["away_goals"]) if venue == "home" else (m["away_goals"], m["home_goals"])
        opp = m["away"] if venue == "home" else m["home"]
        out.append({"date": m["date"][:10], "venue": venue, "gf": gf, "ga": ga, "opp": opp})
    return out


def h2h_games(prior: list, home: str, away: str, before: datetime.datetime, n: int = 8) -> list:
    games = [m for m in prior if {m["home"], m["away"]} == {home, away}
             and datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00")) < before]
    out = []
    for m in games[-n:]:
        out.append({"date": m["date"][:10], "home": m["home"], "away": m["away"],
                     "home_goals": m["home_goals"], "away_goals": m["away_goals"]})
    return out


def attackers(state: dict, team_id: int, min_starts: int = 3, top_n: int = 3) -> list:
    lineup_hist = state["team_lineup_history"][team_id]
    starter_counts: dict[int, int] = defaultdict(int)
    for lineup_set in lineup_hist:
        for pid in lineup_set:
            starter_counts[pid] += 1
    names = squad_names(team_id)
    candidates = []
    for pid, count in starter_counts.items():
        pos = state["player_positions"].get(pid)
        if pos not in ATTACKING_POS:
            continue
        goal_hist = state["player_goal_history"].get(pid)
        if not goal_hist or len(goal_hist) < min_starts:
            continue
        goals_sum = sum(goal_hist)
        candidates.append({
            "player_id": pid,
            "name": names.get(pid, f"player #{pid}"),
            "starts_in_window": count,
            "starts_tracked": len(goal_hist),
            "goals_in_tracked": goals_sum,
            "goals_per_start": goals_sum / len(goal_hist),
        })
    candidates.sort(key=lambda c: (-c["starts_in_window"], -c["goals_per_start"]))
    return candidates[:top_n]


def main() -> None:
    print("Fetching full fixture history (cached)...")
    all_finished = fetch_all_fixtures(None)

    print("Replaying full history into team state (one pass for all fixtures)...")
    state = new_state()
    for i, m in enumerate(all_finished, start=1):
        apply_match(state, m)
        if i % 5000 == 0:
            print(f"  ...{i}/{len(all_finished)}")

    report = []
    for comp, fid, home, home_id, away, away_id, date_str in FIXTURES:
        print(f"\nPulling evidence for {home} vs {away} ({comp})...")
        before = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        prior = [m for m in all_finished if datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00")) < before]
        prior.sort(key=lambda m: m["date"])
        season = 2026

        entry = {
            "competition": comp, "fixture_id": fid, "date": date_str,
            "home": home, "home_id": home_id, "away": away, "away_id": away_id,
            "home_venue_games": venue_games(prior, home, "home", before),
            "away_venue_games": venue_games(prior, away, "away", before),
            "home_season_games": season_games(prior, home, comp, season, before),
            "away_season_games": season_games(prior, away, comp, season, before),
            "h2h": h2h_games(prior, home, away, before),
            "home_attackers": attackers(state, home_id),
            "away_attackers": attackers(state, away_id),
        }
        report.append(entry)

    with open("data/report_evidence.json", "w") as f:
        json.dump(report, f, indent=2)
    print("\nWrote data/report_evidence.json")


if __name__ == "__main__":
    main()
