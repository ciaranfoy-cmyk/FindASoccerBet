"""Tiny stdlib HTTP helper: per-host throttling, a browser-ish User-Agent, JSON/text GET."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "crypto-radar/0.1 (personal research script)"

# Minimum seconds between requests to the same host. Keeps us polite and
# under the free-tier limits (Reddit ~10/min unauthenticated, CoinGecko ~10/min).
_HOST_INTERVAL = {
    "www.reddit.com": 12.0,  # RSS without an app: 429s if faster
    "oauth.reddit.com": 1.0,
    "api.coingecko.com": 6.5,
    "a.4cdn.org": 1.1,
    "t.me": 1.5,
    "api.dexscreener.com": 1.0,
}
_DEFAULT_INTERVAL = 1.0
_last_request_at: dict[str, float] = {}


class HttpError(RuntimeError):
    pass


def _throttle(host: str) -> None:
    interval = _HOST_INTERVAL.get(host, _DEFAULT_INTERVAL)
    elapsed = time.monotonic() - _last_request_at.get(host, 0.0)
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _last_request_at[host] = time.monotonic()


def request(url: str, headers: dict | None = None, data: bytes | None = None,
            timeout: float = 20, retries: int = 2) -> bytes:
    host = urllib.parse.urlparse(url).netloc
    all_headers = {"User-Agent": USER_AGENT}
    all_headers.update(headers or {})
    for attempt in range(retries + 1):
        _throttle(host)
        req = urllib.request.Request(url, headers=all_headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                # Rate limited: wait as told (capped), then retry.
                try:
                    wait = float(exc.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0
                time.sleep(min(max(wait, 20 * (attempt + 1)), 60))
                continue
            body = exc.read().decode(errors="replace")[:300]
            raise HttpError(f"{url} -> {exc.code} {exc.reason}: {body}") from exc
        except urllib.error.URLError as exc:
            raise HttpError(f"Could not reach {url}: {exc.reason}") from exc
    raise AssertionError("unreachable")


def get_json(url: str, headers: dict | None = None):
    return json.loads(request(url, headers=headers))


def get_text(url: str, headers: dict | None = None) -> str:
    return request(url, headers=headers).decode("utf-8", errors="replace")
