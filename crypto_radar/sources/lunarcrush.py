"""LunarCrush (API v4): social metrics across crypto X, Reddit, YouTube, TikTok, news.
Optional: needs LUNARCRUSH_API_KEY.

One request per check: the coin list sorted by AltRank (LunarCrush's blend of price
action and social activity; 1 = best). We keep the top N with their social volume,
interactions, social dominance and sentiment, as a "what's trending across crypto
social" list that covers far more of X than we can afford to read ourselves.
"""

import json
import os
import urllib.parse

from .. import net

BASE = "https://lunarcrush.com/api4/public/coins/list/{version}"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_coins(data: dict) -> list[dict]:
    rows = data.get("data") if isinstance(data, dict) else data
    out = []
    for c in rows or []:
        sym = (c.get("symbol") or "").upper()
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "name": c.get("name") or "",
            "alt_rank": _num(c.get("alt_rank")),
            "galaxy_score": _num(c.get("galaxy_score")),
            "interactions_24h": _num(c.get("interactions_24h")),
            "social_volume_24h": _num(c.get("social_volume_24h")),
            "social_dominance": _num(c.get("social_dominance")),
            "sentiment": _num(c.get("sentiment")),          # % positive
            "change_24h": _num(c.get("percent_change_24h")),
            "market_cap": _num(c.get("market_cap")),
        })
    return out


def fetch(limit: int = 50) -> list[dict]:
    key = os.environ.get("LUNARCRUSH_API_KEY", "").strip()
    if not key:
        return []
    params = urllib.parse.urlencode({"sort": "alt_rank", "limit": limit})
    last_exc = None
    for version in ("v2", "v1"):  # v2 is current; fall back if the plan only has v1
        try:
            data = net.get_json(f"{BASE.format(version=version)}?{params}",
                                headers={"Authorization": f"Bearer {key}"})
        except net.HttpError as exc:
            last_exc = exc
            continue
        coins = parse_coins(data)
        if coins:
            coins.sort(key=lambda c: c["alt_rank"] if c["alt_rank"] is not None else 1e9)
            return coins[:limit]
    if last_exc:
        print(f"[lunarcrush] {str(last_exc)[:200]}")
    return []


def detail(c: dict) -> str:
    """Compact JSON kept in search_trends.detail for the report."""
    keep = ("alt_rank", "galaxy_score", "interactions_24h", "social_volume_24h",
            "social_dominance", "sentiment", "change_24h")
    return json.dumps({k: c[k] for k in keep if c.get(k) is not None})
