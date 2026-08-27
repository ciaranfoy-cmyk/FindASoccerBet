"""Thin client for the football-data.org v4 API, with on-disk caching.

Requires a free API key from https://www.football-data.org/client/register,
passed via the FOOTBALL_DATA_API_KEY environment variable.
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

API_BASE = "https://api.football-data.org/v4"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")

# Free tier allows 10 requests/minute; stay comfortably under that.
_MIN_REQUEST_INTERVAL = 6.5
_last_request_at = 0.0


class FootballDataError(RuntimeError):
    pass


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _api_key() -> str:
    key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not key:
        raise FootballDataError(
            "Missing FOOTBALL_DATA_API_KEY environment variable. "
            "Get a free key at https://www.football-data.org/client/register"
        )
    return key


def _cache_path(path: str, params: dict) -> str:
    key = path + "?" + json.dumps(params, sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    safe_name = path.strip("/").replace("/", "_")
    return os.path.join(CACHE_DIR, f"{safe_name}_{digest}.json")


def get(path: str, params: dict | None = None, ttl_seconds: float | None = None) -> dict:
    """GET a football-data.org v4 endpoint, with on-disk caching.

    ttl_seconds=None caches forever (finished-season data never changes).
    ttl_seconds=0 always makes a fresh request (e.g. for upcoming fixtures).
    """
    params = params or {}
    cache_file = _cache_path(path, params)

    if ttl_seconds != 0 and os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if ttl_seconds is None or age < ttl_seconds:
            with open(cache_file) as f:
                return json.load(f)

    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API_BASE}{path}"
    if query:
        url += f"?{query}"

    _throttle()
    request = urllib.request.Request(url, headers={"X-Auth-Token": _api_key()})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise FootballDataError(f"API request failed: {exc.code} {exc.reason} — {body}") from exc
    except urllib.error.URLError as exc:
        raise FootballDataError(f"Could not reach {API_BASE}: {exc.reason}") from exc

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(data, f)

    return data


def recent_completed_seasons(n: int = 3, competition: str = "PL") -> list[int]:
    """Return the start-years of the n most recently completed seasons."""
    data = get(f"/competitions/{competition}", ttl_seconds=86400)
    current_year = int(data["currentSeason"]["startDate"][:4])
    return [current_year - i for i in range(n, 0, -1)]


def next_matchday(competition: str = "PL") -> int:
    """Return the matchday number of the soonest not-yet-played fixture.

    Filtering the matches endpoint by status alone has proven unreliable
    (returns an incomplete result set), so this fetches the full season
    schedule and filters client-side instead.
    """
    data = get(f"/competitions/{competition}/matches", ttl_seconds=300)
    matches = [m for m in data.get("matches", []) if m["status"] != "FINISHED"]
    if not matches:
        raise FootballDataError("No upcoming matches found; pass --matchday explicitly.")
    matches.sort(key=lambda m: m["utcDate"])
    return matches[0]["matchday"]
