#!/usr/bin/env python3
"""Renders the 'real games and players' evidence panel HTML for every
fixture in data/report_evidence.json, matching the hand-built Lens vs
Lorient panel's markup exactly. Prints one <div class="evidence">...</div>
block per fixture, keyed by "HOME vs AWAY" so they can be dropped into
the report by hand.
"""
import html
import json

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(d: str) -> str:
    y, m, day = d.split("-")
    return f"{MONTHS[int(m)]} {int(day)}"


def fmt_date_full(d: str) -> str:
    y, m, day = d.split("-")
    return f"{MONTHS[int(m)]} {y}"


def esc(s) -> str:
    return html.escape(str(s))


def venue_table(games: list, team: str, venue: str) -> str:
    rows = []
    for g in games:
        result = f"{esc(g['home'])} {g['home_goals']}&ndash;{g['away_goals']} {esc(g['away'])}"
        xg = f"{g['xg']:.2f}" if g["xg"] is not None else "&ndash;"
        rows.append(
            f"<tr><td>{fmt_date(g['date'])}</td><td>{result}</td><td>{g['shots'] if g['shots'] is not None else '&ndash;'}</td>"
            f"<td>{g['on_target'] if g['on_target'] is not None else '&ndash;'}</td><td>{g['inside_box'] if g['inside_box'] is not None else '&ndash;'}</td><td>{xg}</td></tr>"
        )
    label = "home" if venue == "home" else "away"
    return (
        f'<div class="esub">{esc(team)}\'s last {len(games)} {label} games</div>\n'
        f'<div class="etable-wrap"><table class="etable">\n'
        f'<tr><th>Date</th><th>Result</th><th>Shots</th><th>On Target</th><th>In Box</th><th>xG</th></tr>\n'
        + "\n".join(rows) + "\n</table></div>"
    )


def season_table(games: list, team: str) -> str:
    n = len(games)
    rows = []
    for g in games[-6:]:
        venue_tag = "(h)" if g["venue"] == "home" else "(a)"
        prep = "vs" if g["venue"] == "home" else "at"
        rows.append(f"<tr><td>{fmt_date(g['date'])}</td><td>{esc(team)} {venue_tag} {g['gf']}&ndash;{g['ga']} {prep} {esc(g['opp'])}</td></tr>")
    thin_note = ""
    if n <= 3:
        thin_note = f'<div class="xg-note" style="border-top:none;padding-top:0;">Only {n} game{"s" if n != 1 else ""} played &mdash; this early-season average carries real but thin evidence.</div>'
    shown_note = f"<!-- showing last {min(n,6)} of {n} -->" if n > 6 else ""
    return (
        f'<div class="esub">{esc(team)}\'s 2026&ndash;27 season so far ({n} game{"s" if n != 1 else ""})</div>\n'
        f'{shown_note}<div class="etable-wrap"><table class="etable">\n<tr><th>Date</th><th>Result</th></tr>\n'
        + "\n".join(rows) + f"\n</table></div>\n{thin_note}"
    )


def h2h_table(games: list, home: str, away: str) -> str:
    if not games:
        return f'<div class="esub">{esc(home)} vs {esc(away)} &mdash; head-to-head</div>\n<div class="xg-note" style="border-top:none;padding-top:0;">No meetings in tracked history &mdash; h2h feature falls back to the league-wide average.</div>'
    rows = []
    for g in games:
        total = g["home_goals"] + g["away_goals"]
        rows.append(f"<tr><td>{fmt_date_full(g['date'])}</td><td>{esc(g['home'])} {g['home_goals']}&ndash;{g['away_goals']} {esc(g['away'])}</td><td>{total}</td></tr>")
    return (
        f'<div class="esub">{esc(home)} vs {esc(away)} &mdash; head-to-head, last {len(games)} meetings</div>\n'
        f'<div class="etable-wrap"><table class="etable">\n<tr><th>Date</th><th>Result</th><th>Total</th></tr>\n'
        + "\n".join(rows) + "\n</table></div>"
    )


def attacker_card(a: dict, team: str) -> str:
    role = "Forward" if a["goals_per_start"] >= 0 else "Attacker"
    return (
        '<div class="p">'
        f'<div class="pname">{esc(a["name"])}</div>'
        f'<div class="prole">{esc(team)}</div>'
        f'<div class="pstat">Started {a["starts_in_window"]} of last 15 team lineups &middot; '
        f'<b>{a["goals_in_tracked"]} goal{"s" if a["goals_in_tracked"] != 1 else ""}</b> in his last {a["starts_tracked"]} starts</div>'
        '</div>'
    )


def attacker_spotlight(home_att: list, away_att: list, home: str, away: str) -> str:
    if not home_att and not away_att:
        return ""
    # Pick by actual scoring output (goals_per_start), not by who started the
    # most games -- "who's driving the attack" means goals, and a low-scoring
    # regular starter (e.g. a defensive midfielder tagged as an attacking
    # position) isn't that even if he plays every week.
    h = max(home_att, key=lambda a: a["goals_per_start"]) if home_att else None
    a = max(away_att, key=lambda a: a["goals_per_start"]) if away_att else None
    cards = []
    if h:
        cards.append(attacker_card(h, home))
    if a:
        cards.append(attacker_card(a, away))
    if not cards:
        return ""
    return (
        '<div class="esub">Attacking form spotlight &mdash; who\'s actually driving it</div>\n'
        f'<div class="player-card">{"".join(cards)}</div>'
    )


def render_fixture(e: dict) -> str:
    home, away = e["home"], e["away"]
    parts = [
        venue_table(e["home_venue_games"], home, "home"),
        venue_table(e["away_venue_games"], away, "away"),
        season_table(e["home_season_games"], home),
        season_table(e["away_season_games"], away),
        h2h_table(e["h2h"], home, away),
        attacker_spotlight(e["home_attackers"], e["away_attackers"], home, away),
    ]
    body = "\n\n".join(p for p in parts if p)
    return (
        '<div class="evidence">\n'
        '<div class="elabel">The real games and players behind these numbers</div>\n\n'
        f"{body}\n"
        "</div>"
    )


def main() -> None:
    data = json.load(open("data/report_evidence.json"))
    for e in data:
        key = f"{e['home']} vs {e['away']}"
        print(f"===EVIDENCE_START:{key}===")
        print(render_fixture(e))
        print(f"===EVIDENCE_END===\n")


if __name__ == "__main__":
    main()
