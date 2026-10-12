"""Thin client for api-sports.io (API-Football) v3, with on-disk caching.

Requires an API key from https://api-sports.io, sent as the
X-Auth-Token-style header this API actually uses: x-apisports-key. The
key is read from the APIFOOTBALL_KEY environment variable if set,
otherwise from secrets/apifootball.env (gitignored, never committed) --
shell env vars don't persist between separate tool invocations in this
harness, so the on-disk fallback is what makes the key durable across
commands within a session.
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
SECRETS_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets", "apifootball.env")

# The account's per-minute limit is 450; stay comfortably under it.
_MIN_REQUEST_INTERVAL = 60 / 400
_last_request_at = 0.0


class ApiFootballError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("APIFOOTBALL_KEY")
    if key:
        return key
    if os.path.exists(SECRETS_ENV_PATH):
        with open(SECRETS_ENV_PATH) as f:
            for line in f:
                if line.startswith("APIFOOTBALL_KEY="):
                    return line.strip().split("=", 1)[1]
    raise ApiFootballError(
        "Missing APIFOOTBALL_KEY -- set the environment variable or "
        f"put it in {SECRETS_ENV_PATH}"
    )


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


_DETAIL_ENDPOINTS = ("statistics", "lineups", "events")
_IDS_PER_BATCH = 20  # API-Football's cap on /fixtures?ids=


def prefetch_fixture_details(fixture_ids: list[int]) -> int:
    """Warm the per-fixture /fixtures/{statistics,lineups,events} cache for
    many finished fixtures at once.

    /fixtures?ids=a-b-c returns up to 20 fixtures per call with their
    statistics, lineups and events embedded -- the same response arrays
    the per-fixture endpoints return (checked field-for-field) -- so one
    call here replaces up to 60 individual calls. Each embedded array is
    written to exactly the cache file get("/fixtures/<endpoint>",
    {"fixture": id}) reads, so callers don't change. Returns the number
    of API calls made.
    """
    missing = [
        fid for fid in dict.fromkeys(fixture_ids)
        if not all(os.path.exists(_cache_path(f"/fixtures/{ep}", {"fixture": fid})) for ep in _DETAIL_ENDPOINTS)
    ]
    calls = 0
    for i in range(0, len(missing), _IDS_PER_BATCH):
        batch = missing[i:i + _IDS_PER_BATCH]
        params = {"ids": "-".join(str(fid) for fid in batch)}
        data = get("/fixtures", params, ttl_seconds=0)
        calls += 1
        # The combined response is split into per-fixture files below; the
        # batch file itself would never be read again.
        os.remove(_cache_path("/fixtures", params))
        os.makedirs(CACHE_DIR, exist_ok=True)
        for item in data.get("response", []):
            fid = item["fixture"]["id"]
            for ep in _DETAIL_ENDPOINTS:
                arr = item.get(ep) or []
                envelope = {
                    "get": f"fixtures/{ep}",
                    "parameters": {"fixture": str(fid)},
                    "errors": [],
                    "results": len(arr),
                    "paging": {"current": 1, "total": 1},
                    "response": arr,
                }
                with open(_cache_path(f"/fixtures/{ep}", {"fixture": fid}), "w") as f:
                    json.dump(envelope, f)
        if (i // _IDS_PER_BATCH) % 50 == 0:
            print(f"  ...prefetched {min(i + _IDS_PER_BATCH, len(missing))}/{len(missing)} fixtures", flush=True)
    return calls
