#!/usr/bin/env python3
"""Live market-odds feature: fetches a devigged Over 2.5 probability
from Bet365 (via thestatsapi.com) for a single upcoming fixture, for
use as one extra input to the "odds-augmented" model tier in
predict_upcoming.py/explain_picks.py.

DISABLED as of 2026-09-22 (see MARKET_ODDS_ENABLED below) -- kept for
reference, not deleted, in case the underlying data source gets reused
for something else (see the shotmap work started the same day).

WHY IT'S OFF: the feature was real and validated (Brier 0.2428 ->
0.2397, n=5567 -- see history below), but a coefficient inspection of
the actual live-cached models found the xG+odds and core+odds tiers
each had exactly ONE nonzero coefficient out of 92/74 -- mkt_over25_prob
itself. L1 hadn't "added the market as one more signal"; it had
discarded every engineered feature and made the tier a straight,
recalibrated pass-through of Bet365's own price. A forced-blend
retest (L2 penalty, so no coefficient could be driven to exactly
zero) confirmed this wasn't an L1 artifact: the data-optimal blend
weight really is ~86-97% market, because xG's own info is already
priced into Bet365 and adds almost nothing on top of it.

That distinction matters because of WHAT Kalshi/Polymarket turned out
to be: not an independent market, but one that tracks Bet365 closely
(measured directly: mean gap 0.88pp, max 2.5pp across 16 live MLS
fixtures). So a feature that makes our own prediction into "Bet365,
recalibrated" and then gets compared against Kalshi, which is itself
"basically Bet365," produces a model that's accurate (the recalibration
step really did improve Brier/hit-rate) but structurally can't show
edge -- you can't find a mispriced bet by rediscovering the same price
you started from. This is also why the earlier profitability backtest
came back near-breakeven against Bet365 at every threshold: that
wasn't a data-quality problem, it was this mechanism working exactly
as the math predicts.

The core/xG tiers (no odds) are less "accurate" by this same measure,
but that's exactly why they're still useful: their disagreements with
the market are the actual source of any real tradeable edge, and
blending in the market number -- at ANY weight, not just this
architecture's ~95% -- mechanically shrinks that disagreement by
proportionally that same weight. There's no tuning that keeps both.

WHY Bet365, not Kalshi/Polymarket, as the (former) model input: using
the same price being traded against as a model INPUT would be
circular. Bet365 was chosen as an independent, sharper reference --
correct reasoning, it just turned out Bet365 and Kalshi aren't
independent of EACH OTHER either.

VALIDATION HISTORY (for reference): adding this feature to
CORE_CANDIDATES on an early 3-league sample (n=3456) moved Brier
0.2470 -> 0.2430. Scaled to all 13 leagues (n=5567 for xG+odds): Brier
0.2428 -> 0.2397, consistent positive hit-rate gains at every
percentile tier tested (70th-97.5th). All of that was real -- the
recalibration genuinely improves accuracy. It just doesn't produce
edge, for the structural reason above, not a validation failure.

MASTER KILL SWITCH: MARKET_ODDS_ENABLED = False fully disables this --
the live pipeline behaves exactly as it did before this feature
existed (no API calls made). To remove entirely: delete this file and
the few call sites in predict_upcoming.py / explain_picks.py /
calibration.py / model_cache.py that reference it.

Any failure (auth, rate limit, subscription lapsed, no match found, no
odds posted yet for a fixture that far out) returns None -- treated
identically to "no market price available", never raises. Callers must
already handle a missing xG feature the same way (see has_xg in
predict_upcoming.py), so this fits the existing fallback pattern.
"""

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request

MARKET_ODDS_ENABLED = False

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
    "BUNDESLIGA": "comp_4643",
    "LIGUE1": "comp_0256",
    "MLS": "comp_9799",
    "EREDIVISIE": "comp_3809",
    "ELC": "comp_8321",
    "SUPERLIG": "comp_9235",
    "BRASILEIRAO": "comp_4795",
    "LIGAPORTUGAL": "comp_8385",
    "JLEAGUE": "comp_6240",
    # LIGAMX deliberately excluded from live fetching even though its
    # historical data was part of the validation above: thestatsapi
    # splits it into two rotating competition_ids (Apertura/Clausura),
    # so a single static id here would silently point at the wrong,
    # off-season tournament for half of every year. Needs a small
    # season-aware lookup (like CURRENT_SEASON_OVERRIDE elsewhere in
    # this project) before it can be added safely, not just an id.
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


# thestatsapi and our own data disagree on a handful of common short forms
# for the same city/club -- found by inspecting real name-match misses (see
# git history for the specific fixtures this fixed).
_ALIASES = {
    "la": "los angeles",
    "ny": "new york",
    "sf": "san francisco",
    "dc": "district of columbia",
}


def _normalize(name: str) -> str:
    # strip accents (e.g. "Montréal" vs "Montreal") before anything else
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    # turn punctuation into a space rather than deleting it, so
    # "St.Louis" (no space in source) and "St. Louis" (space in source)
    # normalize identically instead of merging into different strings
    name = name.lower().replace(".", " ").replace("-", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return " ".join(_ALIASES.get(w, w) for w in name.split(" "))


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
