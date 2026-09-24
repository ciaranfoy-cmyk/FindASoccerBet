"""Crypto news sites via RSS/Atom. Slower than social, but tells you *why* a coin is moving."""

import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime
from urllib.parse import urlparse

from .. import net
from . import Post

_TAG_RE = re.compile(r"<[^>]+>")
_ATOM = "{http://www.w3.org/2005/Atom}"


def _strip(s: str | None) -> str:
    return html.unescape(_TAG_RE.sub(" ", s or "")).strip()


def _parse_date(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return parsedate_to_datetime(s).timestamp()     # RSS: RFC 822
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()  # Atom: ISO 8601
    except ValueError:
        return 0.0


def parse_feed(xml_text: str, feed_url: str) -> list[Post]:
    root = ET.fromstring(xml_text)
    site = urlparse(feed_url).netloc.removeprefix("www.")
    posts = []
    items = root.iter("item") if root.find(".//item") is not None else root.iter(f"{_ATOM}entry")
    for item in items:
        def field(name):
            el = item.find(name)
            if el is None:
                el = item.find(f"{_ATOM}{name}")
            return el

        title = _strip(getattr(field("title"), "text", ""))
        summary = _strip(getattr(field("description"), "text", None)
                         or getattr(field("summary"), "text", None))
        link_el = field("link")
        link = (link_el.text or link_el.get("href", "")) if link_el is not None else ""
        date = _parse_date(getattr(field("pubDate"), "text", None)
                           or getattr(field("published"), "text", None)
                           or getattr(field("updated"), "text", None))
        if not title:
            continue
        posts.append(Post(
            id=f"news:{link or title}",
            source="news",
            channel=site,
            author=site,
            created_utc=date,
            text=f"{title}\n{summary[:1000]}",
            url=link.strip(),
        ))
    return posts


def collect(feeds: list[str]) -> list[Post]:
    posts: list[Post] = []
    for url in feeds:
        try:
            posts.extend(parse_feed(net.get_text(url), url))
        except (net.HttpError, ET.ParseError) as exc:
            print(f"[news] {url}: {exc}")
    return posts
