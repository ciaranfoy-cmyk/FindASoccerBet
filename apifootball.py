"""Thin client for api-sports.io (API-Football) v3, with on-disk caching.

Requires an API key from https://api-sports.io, passed via the
APIFOOTBALL_KEY environment variable, sent as the X-Auth-Token-style
header this API actually uses: x-apisports-key.
"""

import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request

# Transient network failures worth retrying — RemoteDisconnected (a bare
# socket drop) is NOT a subclass of URLError, so it needs its own clause;
# a single one of these killed an hours-long run with no retry before.
_TRANSIENT_ERRORS = (urllib.error.URLError, ConnectionError, http.client.HTTPException, TimeoutError)
_MAX_RETRIES = 4

API_BASE = "https://v3.football.api-sports.io"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache_apifootball")

# The account's per-minute limit is 450; stay comfortably under it.
_MIN_REQUEST_INTERVAL = 60 / 400
_last_request_at = 0.0


class ApiFootballError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        raise ApiFootballError("Missing APIFOOTBALL_KEY environment variable.")
    return key


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _cache_path(path: str, params: dict) -> str:
    key = path + "?" + json.dumps(params, sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    safe_name = path.strip("/").replace("/", "_")
    return os.path.join(CACHE_DIR, f"{safe_name}_{digest}.json")


def get(path: str, params: dict | None = None, ttl_seconds: float | None = None) -> dict:
    """GET an api-football v3 endpoint, with on-disk caching.

    ttl_seconds=None caches forever (finished-match data never changes).
    ttl_seconds=0 always makes a fresh request.
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

    request = urllib.request.Request(url, headers={"x-apisports-key": _api_key()})

    data = None
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise ApiFootballError(f"API request failed: {exc.code} {exc.reason} — {body}") from exc
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2**attempt)
    if data is None:
        raise ApiFootballError(f"Could not reach {API_BASE} after {_MAX_RETRIES} attempts: {last_exc}")

    if data.get("errors"):
        raise ApiFootballError(f"{path} {params}: {data['errors']}")

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(data, f)

    return data
