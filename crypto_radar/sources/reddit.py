"""Reddit: newest posts and comments from a list of subreddits.

With REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET set (a "script" app from
https://www.reddit.com/prefs/apps, which now needs Reddit's approval) we use
app-only OAuth: read-only, no user login, 100 items per request.

Without credentials we read the public RSS feeds instead. Reddit blocks the
anonymous .json endpoints from datacenter IPs (e.g. GitHub Actions); RSS is
the remaining no-key route.
"""

import base64
import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
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


def _listing(path: str, token: str) -> list[dict]:
    data = net.get_json(f"https://oauth.reddit.com{path}",
                        headers={"Authorization": f"Bearer {token}"})
    return [child["data"] for child in data.get("data", {}).get("children", [])]


_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")


def parse_rss(xml_text: str, subreddit: str) -> list[Post]:
    """Parse a subreddit's Atom feed (/r/<sub>/new/.rss or /r/<sub>/comments/.rss)."""
    posts = []
    for entry in ET.fromstring(xml_text).iter(f"{_ATOM}entry"):
        fullname = entry.findtext(f"{_ATOM}id", "")
        author = entry.findtext(f"{_ATOM}author/{_ATOM}name", "").removeprefix("/u/")
        if not fullname or author in ("", "[deleted]", "AutoModerator"):
            continue
        body = html.unescape(_TAG_RE.sub(" ", entry.findtext(f"{_ATOM}content", "")))
        body = body.split("submitted by")[0]  # posts end with "submitted by /u/x [link] [comments]"
        # Comment titles are "/u/x on <post title>": leave the post's title out so its
        # coins aren't credited to every commenter.
        title = "" if fullname.startswith("t1_") else entry.findtext(f"{_ATOM}title", "")
        text = " ".join(f"{title} {body}".split())
        link = entry.find(f"{_ATOM}link")
        when = entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated", "")
        if not text:
            continue
        posts.append(Post(
            id=f"reddit:{fullname}",
            source="reddit",
            channel=f"r/{subreddit}",
            author=author,
            created_utc=datetime.fromisoformat(when).timestamp() if when else 0.0,
            text=text,
            url=link.get("href", "") if link is not None else "",
        ))
    return posts


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
    try:
        token = _oauth_token()
    except net.HttpError as exc:
        print(f"[reddit] OAuth failed, falling back to RSS: {exc}")
        token = None
    posts: list[Post] = []
    for sub in subreddits:
        for kind in ("new", "comments"):
            try:
                if token:
                    items = _listing(f"/r/{sub}/{kind}.json?limit={limit}&raw_json=1", token)
                    got = [p for p in (parse_listing_item(i, sub) for i in items) if p]
                else:
                    got = parse_rss(net.get_text(
                        f"https://www.reddit.com/r/{sub}/{kind}/.rss?limit={limit}"), sub)
            except (net.HttpError, ET.ParseError) as exc:
                print(f"[reddit] r/{sub}/{kind}: {str(exc)[:150]}")
                continue
            posts.extend(got)
        print(f"[reddit] r/{sub}: {sum(p.channel == f'r/{sub}' for p in posts)} items "
              f"via {'OAuth' if token else 'RSS'}")
    return posts
