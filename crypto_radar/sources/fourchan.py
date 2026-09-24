"""4chan /biz/: the board's catalog (every thread's opening post + latest replies),
plus full reply lists for the busiest threads. Read-only public JSON API, no key.

Posters are anonymous, so "author" is the thread: ten replies in one shill
thread count as one voice, which is what we want for breadth.
"""

import html
import re

from .. import net
from . import Post

API = "https://a.4cdn.org"
_TAG_RE = re.compile(r"<[^>]+>")


def clean_comment(raw: str) -> str:
    text = raw.replace("<br>", "\n")
    text = _TAG_RE.sub("", text)
    text = re.sub(r">>\d+", "", html.unescape(text))  # reply links
    return text.strip()


def _post(board: str, thread_no: int, item: dict) -> Post | None:
    text = " ".join(p for p in (item.get("sub"), item.get("com")) if p)
    text = clean_comment(text)
    if not text:
        return None
    return Post(
        id=f"4chan:{board}:{item['no']}",
        source="4chan",
        channel=f"/{board}/",
        author=f"thread:{thread_no}",
        created_utc=float(item.get("time", 0)),
        text=text,
        url=f"https://boards.4chan.org/{board}/thread/{thread_no}#p{item['no']}",
    )


def parse_catalog(pages: list[dict], board: str) -> list[Post]:
    posts = []
    for page in pages:
        for thread in page.get("threads", []):
            if thread.get("sticky"):
                continue
            for item in [thread] + thread.get("last_replies", []):
                post = _post(board, thread["no"], item)
                if post:
                    posts.append(post)
    return posts


def collect(board: str = "biz", full_threads: int = 15) -> list[Post]:
    try:
        pages = net.get_json(f"{API}/{board}/catalog.json")
    except net.HttpError as exc:
        print(f"[4chan] /{board}/ catalog: {exc}")
        return []
    posts = parse_catalog(pages, board)

    threads = [t for p in pages for t in p.get("threads", []) if not t.get("sticky")]
    threads.sort(key=lambda t: t.get("replies", 0), reverse=True)
    for thread in threads[:full_threads]:
        try:
            data = net.get_json(f"{API}/{board}/thread/{thread['no']}.json")
        except net.HttpError as exc:
            print(f"[4chan] thread {thread['no']}: {exc}")
            continue
        posts.extend(p for p in (_post(board, thread["no"], i) for i in data.get("posts", [])) if p)
    return posts
