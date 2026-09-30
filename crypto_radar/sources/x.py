"""X / Twitter via the official v2 recent-search API. Optional: needs X_BEARER_TOKEN.

X's API is paid (the free tier can't search). Recent search returns up to 100
tweets per request from the last 7 days. Each run picks up from the newest
tweet id seen last time, so you only pay for new tweets.
"""

import os
import re
import urllib.parse
from datetime import datetime

from .. import net
from . import Post

API = "https://api.twitter.com/2/tweets/search/recent"
TRENDS_API = "https://api.twitter.com/2/trends/by/woeid/{woeid}"
# Yahoo "where on earth" ids X uses for trend locations.
WOEIDS = {"WORLD": 1, "UK": 23424975, "US": 23424977}

# Airdrop/giveaway/referral posts are almost all spam bots. Excluded in the search
# itself (so they aren't billed) and again here in case X's matching lets one through.
SPAM_TERMS = ("airdrop", "giveaway", "referral")
SPAM_FILTER = " ".join(f"-{t}" for t in SPAM_TERMS)
_SPAM_RE = re.compile("|".join(SPAM_TERMS), re.I)


def search_query(base: str) -> str:
    """A general search with the spam exclusions added."""
    return base if SPAM_FILTER in base else f"{base} {SPAM_FILTER}"


def is_spam(text: str) -> bool:
    return bool(_SPAM_RE.search(text or ""))


def account_queries(accounts: list[str], max_len: int = 512) -> list[str]:
    """Search queries covering the given accounts: "(from:a OR from:b ...) -is:retweet
    -is:reply", split so each stays within X's query length limit."""
    suffix = f") -is:retweet -is:reply {SPAM_FILTER}"
    queries, current = [], []
    for name in (a.strip().lstrip("@") for a in accounts):
        if not name:
            continue
        candidate = current + [f"from:{name}"]
        if current and len("(" + " OR ".join(candidate) + suffix) > max_len:
            queries.append("(" + " OR ".join(current) + suffix)
            candidate = [f"from:{name}"]
        current = candidate
    if current:
        queries.append("(" + " OR ".join(current) + suffix)
    return queries


def parse_response(data: dict, query: str) -> list[Post]:
    users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}
    watched = "from:" in query
    posts = []
    for tweet in data.get("data", []):
        if is_spam(tweet.get("text", "")):
            continue
        author = users.get(tweet.get("author_id"), tweet.get("author_id", "unknown"))
        created = datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00")).timestamp()
        posts.append(Post(
            id=f"x:{tweet['id']}",
            source="x",
            # Watched accounts are labelled per account (x:@saylor) so they can be
            # told apart in reports and marked as signal accounts.
            channel=f"x:@{author.lower()}" if watched else "x:search",
            author=author,
            created_utc=created,
            text=tweet["text"],
            url=f"https://x.com/{author}/status/{tweet['id']}",
        ))
    return posts


def trends(location: str) -> list[dict]:
    """X's trending topics for a location: [{"name", "posts"}] (one request, not per post)."""
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        return []
    url = TRENDS_API.format(woeid=WOEIDS[location]) + "?max_trends=50&trend.fields=trend_name,tweet_count"
    data = net.get_json(url, headers={"Authorization": f"Bearer {token}"})
    return parse_trends(data)


def parse_trends(data: dict) -> list[dict]:
    return [{"name": t.get("trend_name", ""), "posts": t.get("tweet_count")}
            for t in data.get("data", []) if t.get("trend_name")]


def collect(queries: list[str], since_ids: dict[str, str], max_posts: int = 100) -> tuple[list[Post], int]:
    """since_ids is updated in place with the newest tweet id per query.

    X bills per post read, so `max_posts` caps what one query can pull per run.
    Returns (posts kept, posts read) - the read count includes spam we dropped."""
    token = os.environ.get("X_BEARER_TOKEN")
    if not token or max_posts <= 0:
        return [], 0
    posts: list[Post] = []
    read = 0
    for query in queries:
        page_size = max(10, min(100, max_posts))  # API allows 10-100
        max_pages = max(1, -(-max_posts // page_size))
        params = {"query": query, "max_results": str(page_size),
                  "tweet.fields": "created_at,author_id", "expansions": "author_id"}
        if since_ids.get(query):
            params["since_id"] = since_ids[query]
        newest = None
        for _ in range(max_pages):
            try:
                data = net.get_json(f"{API}?{urllib.parse.urlencode(params)}",
                                    headers={"Authorization": f"Bearer {token}"})
            except net.HttpError as exc:
                print(f"[x] {query!r}: {exc}")
                break
            posts.extend(parse_response(data, query))
            read += len(data.get("data", []))
            meta = data.get("meta", {})
            newest = newest or meta.get("newest_id")
            if not meta.get("next_token"):
                break
            params["next_token"] = meta["next_token"]
        if newest:
            since_ids[query] = newest
    return posts, read
