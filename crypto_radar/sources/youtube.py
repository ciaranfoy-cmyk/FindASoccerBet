"""YouTube via the official Data API v3. Optional: needs YOUTUBE_API_KEY (free).

Per check: each followed channel's latest uploads (title + description become posts),
view/like/comment counts for those videos, and top comments on fresh videos (each
commenter counts as a separate voice). Costs ~1 quota unit per request; the free
allowance is 10,000 units a day.

A video is "taking off" when it is gaining views much faster than that channel's
other recent uploads did (views per hour since upload).
"""

import os
import statistics
import urllib.parse
from datetime import datetime

from .. import net
from . import Post

API = "https://www.googleapis.com/youtube/v3/"


def _get(endpoint: str, key: str, **params) -> dict:
    params["key"] = key
    return net.get_json(API + endpoint + "?" + urllib.parse.urlencode(params))


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def resolve_channels(handles: list[str], key: str, cache: dict) -> dict:
    """@handle -> {"id", "uploads", "title"}; cached (in state) so it's looked up once."""
    for handle in handles:
        h = handle.strip().lstrip("@")
        if not h or h.lower() in cache:
            continue
        try:
            data = _get("channels", key, part="snippet,contentDetails", forHandle=f"@{h}")
        except net.HttpError as exc:
            print(f"[youtube] @{h}: {str(exc)[:150]}")
            continue
        items = data.get("items") or []
        if not items:
            print(f"[youtube] @{h}: no channel with that handle")
            continue
        item = items[0]
        cache[h.lower()] = {"id": item["id"], "title": item["snippet"]["title"],
                            "uploads": item["contentDetails"]["relatedPlaylists"]["uploads"]}
    return cache


def parse_videos(playlist: dict, stats: dict, handle: str, now: float) -> list[dict]:
    """Combine playlistItems + videos responses into video dicts with views/hour."""
    by_id = {v["id"]: v.get("statistics", {}) for v in stats.get("items", [])}
    out = []
    for item in playlist.get("items", []):
        snip = item.get("snippet", {})
        vid = item.get("contentDetails", {}).get("videoId") or snip.get("resourceId", {}).get("videoId")
        if not vid:
            continue
        published = _ts(item.get("contentDetails", {}).get("videoPublishedAt") or snip["publishedAt"])
        st = by_id.get(vid, {})
        views = int(st.get("viewCount", 0) or 0)
        hours = max((now - published) / 3600, 0.25)
        out.append({"id": vid, "handle": handle, "title": snip.get("title", ""),
                    "description": snip.get("description", ""), "published": published,
                    "views": views, "likes": int(st.get("likeCount", 0) or 0),
                    "comments": int(st.get("commentCount", 0) or 0),
                    "views_per_hour": views / hours})
    # Channel norm: median views/hour of its other recent uploads.
    for v in out:
        others = [o["views_per_hour"] for o in out if o is not v]
        norm = statistics.median(others) if others else 0
        v["vs_norm"] = v["views_per_hour"] / norm if norm else None
    return out


def video_post(v: dict) -> Post:
    return Post(id=f"yt:{v['id']}", source="youtube", channel=f"yt:@{v['handle']}",
                author=v["handle"], created_utc=v["published"],
                text=f"{v['title']}\n{v['description'][:2000]}",
                url=f"https://www.youtube.com/watch?v={v['id']}")


def parse_comments(data: dict, handle: str, video_id: str) -> list[Post]:
    posts = []
    for thread in data.get("items", []):
        c = thread.get("snippet", {}).get("topLevelComment", {})
        s = c.get("snippet", {})
        if not c.get("id") or not s.get("textOriginal"):
            continue
        posts.append(Post(id=f"yt:c:{c['id']}", source="youtube", channel=f"yt:@{handle}",
                          author=s.get("authorDisplayName", "?"), created_utc=_ts(s["publishedAt"]),
                          text=s["textOriginal"],
                          url=f"https://www.youtube.com/watch?v={video_id}&lc={c['id']}"))
    return posts


def collect(handles: list[str], cache: dict, now: float, videos_per_channel: int = 10,
            comment_hours: float = 48, comments_per_video: int = 20, max_age_days: float = 14) -> tuple[list[Post], list[dict]]:
    """Returns (posts, videos). `cache` (channel lookups) is updated in place."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return [], []
    resolve_channels(handles, key, cache)
    posts: list[Post] = []
    videos: list[dict] = []
    for handle in (h.strip().lstrip("@").lower() for h in handles):
        ch = cache.get(handle)
        if not ch:
            continue
        try:
            playlist = _get("playlistItems", key, part="snippet,contentDetails",
                            playlistId=ch["uploads"], maxResults=videos_per_channel)
            ids = [i["contentDetails"]["videoId"] for i in playlist.get("items", [])
                   if i.get("contentDetails", {}).get("videoId")]
            stats = _get("videos", key, part="statistics", id=",".join(ids)) if ids else {}
        except net.HttpError as exc:
            print(f"[youtube] @{handle}: {str(exc)[:150]}")
            continue
        # Old uploads still set the channel's usual pace, but only recent ones are kept.
        vids = [v for v in parse_videos(playlist, stats, handle, now)
                if now - v["published"] <= max_age_days * 86400]
        videos += vids
        posts += [video_post(v) for v in vids]
        for v in vids:
            if now - v["published"] > comment_hours * 3600 or not v["comments"]:
                continue
            try:
                data = _get("commentThreads", key, part="snippet", videoId=v["id"],
                            order="relevance", maxResults=comments_per_video, textFormat="plainText")
            except net.HttpError as exc:  # comments disabled etc.
                print(f"[youtube] comments {v['id']}: {str(exc)[:120]}")
                continue
            posts += parse_comments(data, handle, v["id"])
    print(f"[youtube] {len(videos)} videos from {len({v['handle'] for v in videos})} channels, "
          f"{sum(1 for p in posts if p.id.startswith('yt:c:'))} comments")
    return posts, videos
