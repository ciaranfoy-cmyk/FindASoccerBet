#!/usr/bin/env python3
"""Fetch Premier League fixtures from the football-data.org API.

Requires a free API key from https://www.football-data.org/client/register,
passed via the FOOTBALL_DATA_API_KEY environment variable.

Usage:
    FOOTBALL_DATA_API_KEY=xxxx python3 fetch_matches.py --matchday 2
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.football-data.org/v4"


def fetch_matches(competition: str, matchday: int, api_key: str) -> dict:
    url = f"{API_BASE}/competitions/{competition}/matches?matchday={matchday}"
    request = urllib.request.Request(url, headers={"X-Auth-Token": api_key})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def format_match(match: dict) -> str:
    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    kickoff = match["utcDate"]
    status = match["status"]
    score = match.get("score", {}).get("fullTime", {})
    home_score, away_score = score.get("home"), score.get("away")

    if home_score is not None and away_score is not None:
        result = f"{home_score}-{away_score}"
    else:
        result = "vs"

    return f"{kickoff}  {home} {result} {away}  [{status}]"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competition", default="PL", help="Competition code, default PL")
    parser.add_argument("--matchday", type=int, default=2, help="Matchday/matchweek number")
    args = parser.parse_args()

    api_key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not api_key:
        print("Missing FOOTBALL_DATA_API_KEY environment variable.", file=sys.stderr)
        print("Get a free key at https://www.football-data.org/client/register", file=sys.stderr)
        return 1

    try:
        data = fetch_matches(args.competition, args.matchday, api_key)
    except urllib.error.HTTPError as exc:
        print(f"API request failed: {exc.code} {exc.reason}", file=sys.stderr)
        print(exc.read().decode(errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Could not reach {API_BASE}: {exc.reason}", file=sys.stderr)
        return 1

    matches = data.get("matches", [])
    if not matches:
        print(f"No matches found for {args.competition} matchday {args.matchday}.")
        return 0

    print(f"{args.competition} — matchday {args.matchday} ({len(matches)} matches)\n")
    for match in matches:
        print(format_match(match))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
