"""Telegram: public channels via their web preview (https://t.me/s/<channel>).

No account, API key or login needed, but only *public channels* have a web
preview (not groups or private channels). Each page is the ~20 newest
messages; we page back with ?before=<id> until we reach `max_age_hours`.

For groups/private channels you'd need a user-account client like Telethon;
that's a possible later addition.
"""

import re
import time
from datetime import datetime
from html.parser import HTMLParser

from .. import net
from . import Post


class _PreviewParser(HTMLParser):
    """Extract (post_id, datetime, text) for each message on a t.me/s/ page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.messages: list[dict] = []
        self._current: dict | None = None
        self._text_depth = 0     # >0 while inside the message-text div
        self._div_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "div":
            self._div_depth += 1
            if "tgme_widget_message" in classes and attrs.get("data-post"):
                self._current = {"post": attrs["data-post"], "text": [], "time": None}
                self.messages.append(self._current)
            elif "tgme_widget_message_text" in classes and self._current is not None:
                self._text_depth = self._div_depth
        elif tag == "br" and self._text_depth:
            self._current["text"].append("\n")
        elif tag == "time" and self._current is not None and attrs.get("datetime"):
            self._current["time"] = attrs["datetime"]
        elif tag == "img" and self._text_depth and attrs.get("alt"):
            self._current["text"].append(attrs["alt"])  # custom emoji are <img alt="🚀">

    def handle_endtag(self, tag):
        if tag == "div":
            if self._text_depth and self._div_depth == self._text_depth:
                self._text_depth = 0
            self._div_depth -= 1

    def handle_data(self, data):
        if self._text_depth and self._current is not None:
            self._current["text"].append(data)


def parse_preview(html: str, channel: str) -> list[Post]:
    parser = _PreviewParser()
    parser.feed(html)
    posts = []
    for msg in parser.messages:
        text = "".join(msg["text"]).strip()
        if not text or not msg["time"]:
            continue
        created = datetime.fromisoformat(msg["time"]).timestamp()
        posts.append(Post(
            id=f"telegram:{msg['post']}",
            source="telegram",
            channel=f"t.me/{channel}",
            author=channel,
            created_utc=created,
            text=text,
            url=f"https://t.me/{msg['post']}",
        ))
    return posts


def _message_number(post: Post) -> int:
    return int(re.search(r"/(\d+)$", post.id).group(1))


def collect(channels: list[str], max_age_hours: float = 24, max_pages: int = 5) -> list[Post]:
    cutoff = time.time() - max_age_hours * 3600
    posts: list[Post] = []
    for channel in channels:
        channel = channel.lstrip("@").removeprefix("https://t.me/").strip("/")
        url = f"https://t.me/s/{channel}"
        before = len(posts)
        for _ in range(max_pages):
            try:
                page = parse_preview(net.get_text(url), channel)
            except net.HttpError as exc:
                print(f"[telegram] {channel}: {exc}")
                break
            if not page:
                break
            posts.extend(p for p in page if p.created_utc >= cutoff)
            oldest = min(page, key=lambda p: p.created_utc)
            if oldest.created_utc < cutoff:
                break
            url = f"https://t.me/s/{channel}?before={_message_number(oldest)}"
        got = len(posts) - before
        print(f"[telegram] {channel}: {got} posts in last {max_age_hours:g}h"
              + ("  <- empty: not a public channel, private, or inactive" if not got else ""))
    return posts
