"""X / Twitter via the official v2 recent-search API. Optional: needs X_BEARER_TOKEN.

X's API is paid (the free tier can't search). Recent search returns up to 100
tweets per request from the last 7 days. Each run picks up from the newest
tweet id seen last time, so you only pay for new tweets.
"""

import os
import urllib.parse
from datetime import datetime

from .. import net
from . import Post

API = "https://api.twitter.com/2/tweets/search/recent"


def parse_response(data: dict, query: str) -> list[Post]:
    users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}
    posts = []
    for tweet in data.get("data", []):
        author = users.get(tweet.get("author_id"), tweet.get("author_id", "unknown"))
        created = datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00")).timestamp()
        posts.append(Post(
            id=f"x:{tweet['id']}",
            source="x",
            channel=f"x:{query}",
            author=author,
            created_utc=created,
            text=tweet["text"],
            url=f"https://x.com/{author}/status/{tweet['id']}",
        ))
    return posts


def collect(queries: list[str], since_ids: dict[str, str], max_pages: int = 3) -> list[Post]:
    """since_ids is updated in place with the newest tweet id per query."""
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        return []
    posts: list[Post] = []
    for query in queries:
        params = {"query": query, "max_results": "100",
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
            meta = data.get("meta", {})
            newest = newest or meta.get("newest_id")
            if not meta.get("next_token"):
                break
            params["next_token"] = meta["next_token"]
        if newest:
            since_ids[query] = newest
    return posts
