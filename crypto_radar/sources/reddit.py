"""Reddit: newest posts and comments from a list of subreddits.

Works without credentials via the public .json endpoints (rate-limited, and
Reddit sometimes blocks anonymous clients). For reliability, create a free
"script" app at https://www.reddit.com/prefs/apps and set REDDIT_CLIENT_ID /
REDDIT_CLIENT_SECRET; we then use app-only OAuth (read-only, no user login).
"""

import base64
import json
import os
import time
import urllib.parse

from .. import net
from . import Post

_token: tuple[str, float] | None = None  # (access_token, expires_at)


def _oauth_token() -> str | None:
    global _token
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not (client_id and secret):
        return None
    if _token and _token[1] > time.time() + 60:
        return _token[0]
    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    resp = json.loads(net.request("https://www.reddit.com/api/v1/access_token",
                                   headers={"Authorization": f"Basic {auth}"}, data=data))
    _token = (resp["access_token"], time.time() + resp.get("expires_in", 3600))
    return _token[0]


def _listing(path: str) -> list[dict]:
    token = _oauth_token()
    if token:
        url = f"https://oauth.reddit.com{path}"
        data = net.get_json(url, headers={"Authorization": f"Bearer {token}"})
    else:
        data = net.get_json(f"https://www.reddit.com{path}")
    return [child["data"] for child in data.get("data", {}).get("children", [])]


def parse_listing_item(item: dict, subreddit: str) -> Post | None:
    fullname = item.get("name")  # t3_ = post, t1_ = comment
    if not fullname:
        return None
    text = " ".join(p for p in (item.get("title"), item.get("selftext"), item.get("body")) if p)
    if not text.strip() or item.get("author") in (None, "[deleted]", "AutoModerator"):
        return None
    return Post(
        id=f"reddit:{fullname}",
        source="reddit",
        channel=f"r/{subreddit}",
        author=item["author"],
        created_utc=float(item.get("created_utc", 0)),
        text=text,
        url="https://www.reddit.com" + item.get("permalink", ""),
    )


def collect(subreddits: list[str], limit: int = 100) -> list[Post]:
    posts: list[Post] = []
    for sub in subreddits:
        for kind in ("new", "comments"):
            try:
                items = _listing(f"/r/{sub}/{kind}.json?limit={limit}&raw_json=1")
            except net.HttpError as exc:
                print(f"[reddit] r/{sub}/{kind}: {exc}")
                continue
            posts.extend(p for p in (parse_listing_item(i, sub) for i in items) if p)
    return posts
