"""Search interest: what people are *searching* for, not just posting about.

Two free, keyless feeds, snapshotted every run so we can see coins enter, climb
and stay on the lists:

  CoinGecko trending   top ~15 coins searched on CoinGecko in the last 24h.
                       Crypto-native searchers: an early "people are looking" signal.
  Google Trends RSS    fastest-rising Google searches (UK + US). Crypto rarely
                       makes it; when a coin does, it has gone mainstream.

Google Trends has no official API and unofficial per-keyword scrapers get
blocked from CI, so we only read its public "trending now" feed.
"""

import re
import time
import xml.etree.ElementTree as ET

from . import net
from .coins import Coin
from .extract import COMMON_WORDS, STABLECOINS

COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"

# Words that suggest a trending Google query is about crypto, for tickers/names
# that are also ordinary words.
_CRYPTO_CONTEXT = re.compile(r"\b(coin|crypto|token|price|etf|blockchain|memecoin|airdrop)\b", re.I)


def parse_coingecko_trending(data: dict) -> list[dict]:
    """[{coin_key, symbol, name, rank}] in list order (rank 1 = most searched)."""
    out = []
    for i, entry in enumerate(data.get("coins", []), start=1):
        item = entry.get("item", {})
        if not item.get("id"):
            continue
        symbol = (item.get("symbol") or "").upper()
        if symbol in STABLECOINS:
            continue
        out.append({"coin_key": item["id"], "symbol": symbol, "name": item.get("name", ""),
                    "rank": i, "detail": f"mcap rank {item.get('market_cap_rank') or '?'}"})
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_google_trends(xml_text: str) -> list[dict]:
    """[{query, traffic, news}] from a Google Trends 'trending now' RSS feed."""
    out = []
    for item in ET.fromstring(xml_text).iter("item"):
        query, traffic, news = "", "", []
        for el in item.iter():
            tag = _local(el.tag)
            if tag == "title" and not query:
                query = (el.text or "").strip()
            elif tag == "approx_traffic":
                traffic = (el.text or "").strip()
            elif tag == "news_item_title" and el.text:
                news.append(el.text.strip())
        if query:
            out.append({"query": query, "traffic": traffic, "news": news})
    return out


class GoogleMatcher:
    """Map a trending Google query to a coin. Conservative: a false 'bitcoin is trending
    on Google' would be worse than a miss."""

    def __init__(self, registry: list[Coin], max_rank: int = 300):
        self.by_word: dict[str, Coin] = {}
        for coin in sorted(registry, key=lambda c: c.rank, reverse=True):
            if coin.rank > max_rank or coin.symbol in STABLECOINS:
                continue
            self.by_word[coin.name.lower()] = coin
            self.by_word[coin.symbol.lower()] = coin

    def match(self, query: str, news: list[str]) -> Coin | None:
        q = query.lower()
        context = bool(_CRYPTO_CONTEXT.search(q) or any(_CRYPTO_CONTEXT.search(n) for n in news))
        for word, coin in sorted(self.by_word.items(), key=lambda kv: -len(kv[0])):
            if not re.search(rf"(?<!\w){re.escape(word)}(?!\w)", q):
                continue
            distinctive = (word == coin.name.lower() and len(word) >= 5
                           and word not in COMMON_WORDS)
            if distinctive or context:
                return coin
        return None


def collect(store, registry: list[Coin], geos: list[str]) -> int:
    """Snapshot both feeds into the store. Returns number of rows saved."""
    now = time.time()
    rows = 0
    try:
        for r in parse_coingecko_trending(net.get_json(COINGECKO_TRENDING)):
            store.add_search_trend(now, "coingecko", r["coin_key"], r["rank"],
                                   r["symbol"], r["name"], r["detail"])
            rows += 1
    except net.HttpError as exc:
        print(f"[search] CoinGecko trending: {exc}")

    matcher = GoogleMatcher(registry)
    for geo in geos:
        try:
            trends = parse_google_trends(net.get_text(GOOGLE_TRENDS_RSS.format(geo=geo)))
        except (net.HttpError, ET.ParseError) as exc:
            print(f"[search] Google Trends {geo}: {str(exc)[:150]}")
            continue
        for i, t in enumerate(trends, start=1):
            coin = matcher.match(t["query"], t["news"])
            if coin:
                store.add_search_trend(now, f"google-{geo}", coin.id, i, coin.symbol, coin.name,
                                       f'"{t["query"]}" {t["traffic"]}'.strip())
                rows += 1
    print(f"[search] {rows} trending entries saved")
    return rows
