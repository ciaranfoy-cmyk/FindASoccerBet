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
COINGECKO_MARKETS = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={ids}"
                     "&price_change_percentage=1h,24h")
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
        data = item.get("data") or {}
        change = data.get("price_change_percentage_24h") or {}
        out.append({"coin_key": item["id"], "symbol": symbol, "name": item.get("name", ""),
                    "rank": i, "detail": f"mcap rank {item.get('market_cap_rank') or '?'}",
                    # fallback price info, used if the markets call fails
                    "price": _num(data.get("price")), "change_24h": _num(change.get("usd"))})
    return out


def _num(x):
    try:
        return float(str(x).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_markets(rows: list[dict]) -> dict[str, dict]:
    """coin id -> {price, change_1h, change_24h, market_cap} from /coins/markets."""
    return {r["id"]: {"price": r.get("current_price"),
                      "change_1h": r.get("price_change_percentage_1h_in_currency"),
                      "change_24h": r.get("price_change_percentage_24h_in_currency",
                                          r.get("price_change_percentage_24h")),
                      "market_cap": r.get("market_cap")}
            for r in rows if r.get("id")}


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
        trending = parse_coingecko_trending(net.get_json(COINGECKO_TRENDING))
    except net.HttpError as exc:
        print(f"[search] CoinGecko trending: {exc}")
        trending = []
    for r in trending:
        store.add_search_trend(now, "coingecko", r["coin_key"], r["rank"],
                               r["symbol"], r["name"], r["detail"])
        rows += 1

    # Prices for everything being searched for, so we can tell "searched before it
    # moved" (early) from "searched because it already pumped" (late).
    if trending:
        try:
            markets = parse_markets(net.get_json(COINGECKO_MARKETS.format(
                ids=",".join(r["coin_key"] for r in trending))))
        except net.HttpError as exc:
            print(f"[search] CoinGecko prices: {exc}; using trending-list prices")
            markets = {}
        for r in trending:
            m = markets.get(r["coin_key"]) or {"price": r["price"], "change_24h": r["change_24h"]}
            if m.get("price") is not None:
                store.add_price(now, r["coin_key"], m.get("price"), m.get("change_1h"),
                                m.get("change_24h"), m.get("market_cap"))

    matcher = GoogleMatcher(registry)
    for geo in geos:
        try:
            trends = parse_google_trends(net.get_text(GOOGLE_TRENDS_RSS.format(geo=geo)))
        except (net.HttpError, ET.ParseError) as exc:
            print(f"[search] Google Trends {geo}: {str(exc)[:150]}")
            continue
        print(f"[search] Google Trends {geo}: {len(trends)} trending searches read")
        for i, t in enumerate(trends, start=1):
            coin = matcher.match(t["query"], t["news"])
            if coin:
                store.add_search_trend(now, f"google-{geo}", coin.id, i, coin.symbol, coin.name,
                                       f'"{t["query"]}" {t["traffic"]}'.strip())
                rows += 1
    print(f"[search] {rows} trending entries saved")
    return rows
