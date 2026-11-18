"""Rotation / team-news flags for live picks -- the things the model
can't see when picks are made days before kickoff.

Found in the Nashville v Philadelphia post-mortem (MLS, 2026-10-28, 0-0
on a 71% Over pick): Nashville had already clinched 1st and had another
game three days later, so they benched five usual starters; Philadelphia
were missing both first-choice strikers (one injured, one suspended). The
model scores fixtures off each team's "usual XI", so none of that moved
the number. Re-scored with the real lineups it fell to ~63%, below the bar.

These are warnings, not filters -- none of them has been validated as a
rule. Each fixture gets a list of plain-English flags:

  congestion  -- either side plays another match (any competition) within
                 CONGESTION_DAYS before or after this one.
  settled     -- late in the league season, a side's position can no
                 longer change (or it has clinched 1st) -- nothing to play for.
  missing     -- a usual starter is on API-Football's injury/suspension
                 list for this fixture (posted days ahead), split into new
                 absences (a regular lately, so not in the form numbers yet)
                 and long-term ones (recent form already reflects them),
                 with each player's position.
  lineup      -- once the confirmed XIs are out (~1hr before kickoff),
                 usual starters who were benched or left out.

Live team news changes, so every call here uses a short cache ttl
(TEAM_NEWS_TTL), unlike the forever-cached finished-match endpoints.
"""
from __future__ import annotations

import datetime
from collections import defaultdict

import apifootball
from build_lineup_features import USUAL_XI_MIN_HISTORY
from build_player_form_features import ATTACKING_POS

CONGESTION_DAYS = 4          # another match closer than this either side -> flag
LINEUP_BENCHED_MIN = 3       # this many usual starters missing from the confirmed XI -> flag
TEAM_NEWS_TTL = 1800         # injuries / lineups / schedules can change up to kickoff
STANDINGS_TTL = 6 * 3600
RECENT_WINDOW = 5            # an absentee who started fewer than RECENT_STARTS_MIN of the
RECENT_STARTS_MIN = 2        # team's last RECENT_WINDOW games is a long-term absence
POSITION_NAMES = {"G": "keeper", "D": "defender", "M": "attacker", "F": "attacker"}


def _get(path: str, params: dict, ttl: int) -> dict:
    try:
        return apifootball.get(path, params, ttl_seconds=ttl)
    except apifootball.ApiFootballError:
        return {}


def usual_xi(state: dict, team_id: int) -> set[int]:
    """Same "usual XI" predict_upcoming.attacking_form_for() uses: the
    team's 11 most frequent starters over its tracked lineups."""
    hist = state["team_lineup_history"].get(team_id) or []
    if len(hist) < USUAL_XI_MIN_HISTORY:
        return set()
    freq: dict[int, int] = defaultdict(int)
    for past_xi in hist:
        for pid in past_xi:
            freq[pid] += 1
    return set(sorted(freq, key=lambda p: -freq[p])[:11])


def _short(name: str) -> str:
    return name.split(" (")[0]


def congestion_flags(team: str, team_id: int, season: int, kickoff: datetime.datetime, fixture_id: int) -> list[str]:
    start = (kickoff - datetime.timedelta(days=CONGESTION_DAYS)).date().isoformat()
    end = (kickoff + datetime.timedelta(days=CONGESTION_DAYS)).date().isoformat()
    data = _get("/fixtures", {"team": team_id, "season": season, "from": start, "to": end}, TEAM_NEWS_TTL)
    flags = []
    for m in data.get("response", []):
        if m["fixture"]["id"] == fixture_id:
            continue
        other = datetime.datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00"))
        days = (other - kickoff).total_seconds() / 86400
        if abs(days) >= CONGESTION_DAYS:
            continue
        opp = m["teams"]["away"]["name"] if m["teams"]["home"]["id"] == team_id else m["teams"]["home"]["name"]
        n = int(abs(days))
        gap = f"{n} day{'s' if n != 1 else ''}" if n else "under a day"
        when = f"{gap} later" if days > 0 else f"{gap} earlier"
        flags.append(f"{team} also play {opp} ({m['league']['name']}) {when}")
    return flags


def _remaining_league_games(league_fixtures: list[dict], team_id: int, played: int) -> int | None:
    """League-phase games left for a team: its scheduled league fixtures
    (play-off rounds excluded) minus games already played."""
    total = sum(
        1 for m in league_fixtures
        if team_id in (m["teams"]["home"]["id"], m["teams"]["away"]["id"])
        and "play" not in (m["league"].get("round") or "").lower()
    )
    return max(total - played, 0) if total else None


def settled_flags(team: str, team_id: int, league_id: int, season: int) -> list[str]:
    standings = _get("/standings", {"league": league_id, "season": season}, STANDINGS_TTL)
    fixtures = _get("/fixtures", {"league": league_id, "season": season}, STANDINGS_TTL).get("response", [])
    if not standings.get("response") or not fixtures:
        return []
    for group in standings["response"][0]["league"]["standings"]:
        idx = next((i for i, r in enumerate(group) if r["team"]["id"] == team_id), None)
        if idx is None:
            continue
        row = group[idx]
        left = _remaining_league_games(fixtures, team_id, row["all"]["played"])
        if left is None or left > 5:
            return []

        def max_points(r: dict) -> int:
            r_left = _remaining_league_games(fixtures, r["team"]["id"], r["all"]["played"]) or 0
            return r["points"] + 3 * r_left

        safe_from_below = idx == len(group) - 1 or max_points(group[idx + 1]) < row["points"]
        cant_climb = idx == 0 or max_points(row) < group[idx - 1]["points"]
        if idx == 0 and safe_from_below:
            return [f"{team} have clinched 1st ({row['points']} pts, {left} league games left)"]
        if safe_from_below and cant_climb:
            return [f"{team} can't move from {row['rank']}th ({left} league games left)"]
        return []
    return []


def missing_flags(team: str, team_id: int, fixture_id: int, state: dict) -> list[str]:
    """Usual starters on the injury/suspension list, split into NEW
    absences (a regular starter lately -- the model's form numbers still
    assume he plays, so this can move the real chance) and LONG-TERM ones
    (started fewer than RECENT_STARTS_MIN of the last RECENT_WINDOW games --
    recent form already reflects playing without him). New attackers are
    the ones that matter most for an Over; a missing defender, if
    anything, helps it."""
    usual = usual_xi(state, team_id)
    recent = list(state["team_lineup_history"].get(team_id) or [])[-RECENT_WINDOW:]
    # Once the XIs are posted, a "doubtful" player who starts isn't missing.
    lineups = _get("/fixtures/lineups", {"fixture": fixture_id}, TEAM_NEWS_TTL).get("response", [])
    starting = {p["player"]["id"] for b in lineups if b["team"]["id"] == team_id for p in b.get("startXI", [])}
    data = _get("/injuries", {"fixture": fixture_id}, TEAM_NEWS_TTL)
    seen: set[int] = set()
    new, long_term = [], []
    for e in data.get("response", []):
        p = e["player"]
        if e["team"]["id"] != team_id or p["id"] in seen or p["id"] not in usual or p["id"] in starting:
            continue
        seen.add(p["id"])
        role = POSITION_NAMES.get(state["player_positions"].get(p["id"]), "player")
        doubt = ", doubtful" if p.get("type") == "Questionable" else ""
        starts = sum(p["id"] in xi for xi in recent)
        text = f"{_short(p['name'])} ({role}, {p.get('reason') or 'out'}{doubt}"
        if starts >= RECENT_STARTS_MIN:
            new.append((role != "attacker", f"{text}, started {starts} of last {len(recent)})"))
        else:
            long_term.append(f"{text})")
    flags = []
    if new:
        flags.append(f"{team} NEW absences: " + "; ".join(t for _, t in sorted(new)))
    if long_term:
        flags.append(f"{team} long-term absences, already in recent form: " + "; ".join(long_term))
    return flags


def lineup_flags(team: str, team_id: int, fixture_id: int, state: dict) -> list[str]:
    usual = usual_xi(state, team_id)
    data = _get("/fixtures/lineups", {"fixture": fixture_id}, TEAM_NEWS_TTL)
    block = next((b for b in data.get("response", []) if b["team"]["id"] == team_id), None)
    if not block or not block.get("startXI") or not usual:
        return []
    starters = {p["player"]["id"] for p in block["startXI"]}
    benched = usual - starters
    if len(benched) < LINEUP_BENCHED_MIN:
        return []
    names = {p["player"]["id"]: p["player"]["name"] for p in block.get("substitutes", [])}
    on_bench = [names[pid] for pid in benched if pid in names]
    detail = f" (on the bench: {', '.join(on_bench)})" if on_bench else ""
    return [f"{team} lineup: {len(benched)} of their usual XI not starting{detail}"]


def flags_for(row, state: dict, league_id: int) -> list[str]:
    """All flags for one live_df row (needs fixture_id, date, season,
    home/away team names and ids)."""
    kickoff = datetime.datetime.fromisoformat(str(row["kickoff"]).replace("Z", "+00:00"))
    flags: list[str] = []
    for side in ("home", "away"):
        team, team_id = row[f"{side}_team"], int(row[f"{side}_id"])
        flags += settled_flags(team, team_id, league_id, int(row["season"]))
        flags += congestion_flags(team, team_id, int(row["season"]), kickoff, int(row["fixture_id"]))
        flags += missing_flags(team, team_id, int(row["fixture_id"]), state)
        flags += lineup_flags(team, team_id, int(row["fixture_id"]), state)
    return flags
