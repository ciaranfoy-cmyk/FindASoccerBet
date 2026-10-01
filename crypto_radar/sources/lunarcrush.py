"""LunarCrush (API v4): social metrics across crypto X, Reddit, YouTube, TikTok, news.
Optional: needs LUNARCRUSH_API_KEY.

One request per check: the 200 most-discussed coins (by 24h interactions) with social
volume, social dominance, sentiment and AltRank. Snapshots are kept every run, so the
report can rank coins by how fast their social activity is rising vs a day earlier:
"starting to get attention", not "already pumped" (AltRank leans to the latter).
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


def fetch(limit: int = 200, sort: str = "interactions") -> list[dict]:
    key = os.environ.get("LUNARCRUSH_API_KEY", "").strip()
    if not key:
        return []
    last_exc = None
    for version in ("v2", "v1"):  # v2 is current; fall back if the plan only has v1
        # Try the default order first; if it came back smallest-first, ask for descending.
        for extra in ({}, {"desc": "1"}):
            params = urllib.parse.urlencode({"sort": sort, "limit": limit, **extra})
            try:
                data = net.get_json(f"{BASE.format(version=version)}?{params}",
                                    headers={"Authorization": f"Bearer {key}"})
            except net.HttpError as exc:
                last_exc = exc
                break
            coins = parse_coins(data)
            if not coins:
                break
            vals = [c["interactions_24h"] or 0 for c in coins]
            if extra or vals[0] >= vals[-1]:
                coins.sort(key=lambda c: -(c["interactions_24h"] or 0))
                return coins[:limit]
    if last_exc:
        print(f"[lunarcrush] {str(last_exc)[:200]}")
    return []


def detail(c: dict) -> str:
    """Compact JSON kept in search_trends.detail for the report."""
    keep = ("alt_rank", "galaxy_score", "interactions_24h", "social_volume_24h",
            "social_dominance", "sentiment", "change_24h")
    return json.dumps({k: c[k] for k in keep if c.get(k) is not None})
