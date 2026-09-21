#!/usr/bin/env python3
"""Live market-odds feature: fetches a devigged Over 2.5 probability
from Bet365 (via thestatsapi.com) for a single upcoming fixture, for
use as one extra input to the "odds-augmented" model tier in
predict_upcoming.py/explain_picks.py.

WHY Bet365, not Kalshi/Polymarket: using the same price we're trying to
find edge against as a model INPUT would be circular -- the model
would partly reconstruct Kalshi's own number and then "discover" it
agrees with itself, making any resulting edge fake. Bet365 is an
independent, much larger/sharper market; if it disagrees with Kalshi's
price, that's real information, not a self-fulfilling comparison.

VALIDATED (see this session's real walk-forward tests, PL+LALIGA+SERIEA,
n=3456): adding this feature to CORE_CANDIDATES moved Brier from 0.2470
to 0.2430 with a real, non-zero L1 coefficient (+0.18) -- a genuine
improvement, not noise. A separately-tested "moneyline quality gap"
feature did NOT help (L1 zeroed it every time) and is deliberately not
included here.

MASTER KILL SWITCH: set MARKET_ODDS_ENABLED = False to fully disable --
the live pipeline then behaves exactly as it did before this feature
existed (no API calls made, no behavior change). To remove entirely:
delete this file and the few call sites in predict_upcoming.py /
explain_picks.py / calibration.py / model_cache.py that reference it.

SCOPE: only validated for PL, La Liga, and Serie A so far (the leagues
with enough historical odds volume to backtest). COVERED_COMPETITIONS
below is deliberately narrow -- extending it to another league without
first re-running the same walk-forward validation would be trusting an
untested assumption, not a proven result.

Any failure (auth, rate limit, subscription lapsed, no match found, no
odds posted yet for a fixture that far out) returns None -- treated
identically to "no market price available", never raises. Callers must
already handle a missing xG feature the same way (see has_xg in
predict_upcoming.py), so this fits the existing fallback pattern.
"""

import json
import os
import time
import urllib.error
import urllib.request

MARKET_ODDS_ENABLED = True

API_BASE = "https://api.thestatsapi.com/api"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache_statsapi")
SECRETS_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets", "statsapi.env")
BOOKMAKER = "Bet365"

# competition code (this project's own) -> thestatsapi competition_id.
# Only leagues with a real, validated Brier improvement are listed --
# see module docstring.
COVERED_COMPETITIONS = {
    "PL": "comp_3039",
    "LALIGA": "comp_8814",
    "SERIEA": "comp_5840",
}

_MIN_REQUEST_INTERVAL = 60 / 110  # stay under the paid plan's 120/min cap
_last_request_at = 0.0


class StatsApiError(RuntimeError):
    pass


def _api_key() -> str | None:
    key = os.environ.get("STATSAPI_KEY")
    if key:
        return key
    if os.path.exists(SECRETS_ENV_PATH):
        with open(SECRETS_ENV_PATH) as f:
            for line in f:
                if line.startswith("STATSAPI_KEY="):
                    return line.strip().split("=", 1)[1]
    return None


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _get(path: str, params: dict | None = None) -> dict:
    key = _api_key()
    if not key:
        raise StatsApiError("No STATSAPI_KEY configured")
    cache_key = path.replace("/", "_") + ("_" + "_".join(f"{k}={v}" for k, v in (params or {}).items()) if params else "")
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, cache_key + ".json")
    if os.path.exists(cache_path) and os.path.getmtime(cache_path) > time.time() - 3600:
        with open(cache_path) as f:
            return json.load(f)
    url = API_BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    _throttle()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise StatsApiError(f"HTTP {exc.code} from thestatsapi: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise StatsApiError(f"Could not reach thestatsapi: {exc}") from exc
    with open(cache_path, "w") as f:
        json.dump(data, f)
    return data


def _devig_over(over_o, under_o) -> float | None:
    if not over_o or not under_o:
        return None
    po, pu = 1 / float(over_o), 1 / float(under_o)
    return po / (po + pu)


def _normalize(name: str) -> str:
    return name.lower().replace(".", "").replace("-", " ").strip()


def _find_match_id(competition_id: str, home_team: str, away_team: str, date: str) -> str | None:
    """date: YYYY-MM-DD. Matches by date + fuzzy home/away name overlap --
    thestatsapi's names are often fuller than ours (e.g. "Newcastle
    United" vs our "Newcastle"), so exact match would miss most rows."""
    data = _get("/football/matches", {"competition_id": competition_id, "date_from": date, "date_to": date, "per_page": 100})
    home_norm, away_norm = _normalize(home_team), _normalize(away_team)
    for m in data.get("data", []):
        m_home, m_away = _normalize(m["home_team"]["name"]), _normalize(m["away_team"]["name"])
        home_ok = home_norm in m_home or m_home in home_norm
        away_ok = away_norm in m_away or m_away in away_norm
        if home_ok and away_ok:
            return m["id"]
    return None


def fetch_market_over25_prob(competition: str, home_team: str, away_team: str, date: str) -> float | None:
    """Returns a devigged Over 2.5 probability from Bet365 for this
    fixture, or None if unavailable for ANY reason (wrong league,
    disabled, no match found, no odds posted yet, API failure). date:
    YYYY-MM-DD. Never raises."""
    if not MARKET_ODDS_ENABLED:
        return None
    competition_id = COVERED_COMPETITIONS.get(competition)
    if competition_id is None:
        return None
    try:
        match_id = _find_match_id(competition_id, home_team, away_team, date)
        if match_id is None:
            return None
        odds = _get(f"/football/matches/{match_id}/odds")
        bk = next((b for b in odds["data"]["bookmakers"] if b["bookmaker"] == BOOKMAKER), None)
        if bk is None:
            return None
        tg = bk["markets"].get("total_goals", {})
        line = tg.get("2.5", {})
        return _devig_over(line.get("over", {}).get("last_seen"), line.get("under", {}).get("last_seen"))
    except StatsApiError:
        return None
