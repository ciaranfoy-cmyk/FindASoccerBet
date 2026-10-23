"""What your followed accounts are actually saying, account by account.

X: each followed account's own posts (links stripped, near-duplicates dropped).
YouTube: each new video's title, views vs the channel's usual, the outline from its
description (chapters / bullet points, minus the sponsor and link boilerplate) and a
few top viewer comments with scam replies filtered out."""

from __future__ import annotations

import html
import re
import time
from collections import defaultdict

_URL = re.compile(r"https?://\S+|www\.\S+|t\.co/\S+")
_CHAPTER = re.compile(r"^\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s*[-–—:]?\s*(?P<title>.+)")
_BULLET = re.compile(r"^\s*[•▪◦·\-\*✅✔️📌🔹🔸➡️👉]\s*(.+)")
_PROMO = re.compile(
    r"subscribe|sponsor|discount|bonus|deposit|sign ?up|join (my|our|the|now)|affiliate|use code|"
    r"\bcode\b|promo|coupon|referral|disclaimer|not financial advice|financial advice|beware|scam|"
    r"merch|patreon|newsletter|membership|conference|premium|free trial|exchange partners|"
    r"telegram|discord|instagram|tiktok|twitter|facebook|reddit:|website|inquir|business|"
    r"educational purposes|my products|community|course|link in|links below|timestamps coming|"
    r"#\w+\s+#\w+|ledger|trezor|wallet\W*$|kalshi|bybit|bitunix|btcc|toobit|robinhood|coinbase|"
    r"welcome to|please note|no guarantees|past performance|insurance|\bhsa\b|webull|reinvest|"
    r"@\w+\.\w+|lifetime access|expires|my favorite|get covered|tax deduction|where i trade|kyc|"
    r"skip to|stream start|^\d+ hours?$|win the|teamed up|competition|iphone|school|mexc|copy trading|"
    r"follow|itrust|watch the full|full breakdown|centrali[sz]ed exchange|due diligence|afford to lose|"
    r"highly volatile|invested capital|premier exchange|rated exchange|welcome rewards|tutorial below|"
    r"opinion of|general information purposes|crypto essentials|message you first|private keys|[\U0001D400-\U0001D7FF]",
    re.I)
_COMMENT_SPAM = re.compile(
    r"\$\s?\d[\d,]*\s*(k|profit|in|within)|profit of|made \$|earn(ed)? \$|thank(s| you) (to )?(mr|mrs|ms|miss)|"
    r"whats ?app|telegram|contact (her|him)|mentor|her (strategy|guidance|signals)|his (strategy|guidance)|"
    r"recommend(ed)? (her|him)|\+\d{7,}|investment (manager|adviser|advisor)|financial (adviser|advisor)|"
    r"presale|pre-sale|whitepaper|white paper|airdrop|giveaway|use code|join \w+ at|lifetime access",
    re.I)


_SEPARATOR = re.compile(r"^\s*([▀▄█═─━▬\-_=*~•·●]\s*){5,}$")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{3,}")


def shill_words(store, since: float, min_comments: int = 5, min_channels: int = 3) -> dict[str, int]:
    """Words bot campaigns push in YouTube comment sections: seen in comments on several
    channels but (almost) nowhere else — real coins get discussed on Reddit/X/news too."""
    in_comments: dict[str, set] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    for r in store.db.execute("SELECT channel, text FROM posts WHERE id LIKE 'yt:c:%' AND created_utc >= ?",
                              (since,)):
        for w in {w.lower() for w in _WORD.findall(r["text"] or "")}:
            in_comments[w].add(r["channel"])
            counts[w] += 1
    candidates = [w for w, chans in in_comments.items() if len(chans) >= min_channels and counts[w] >= min_comments]
    shills = {}
    for w in candidates:
        elsewhere = store.db.execute(
            "SELECT COUNT(*) FROM posts WHERE id NOT LIKE 'yt:c:%' AND created_utc >= ? AND LOWER(text) LIKE ?",
            (since, f"%{w}%")).fetchone()[0]
        if elsewhere <= 1:
            shills[w] = counts[w]
    return shills


def copy_paste(store, since: float) -> set[str]:
    """Comment texts posted more than once (bots paste the same line everywhere)."""
    rows = store.db.execute("SELECT text, COUNT(*) n FROM posts WHERE id LIKE 'yt:c:%' AND created_utc >= ? "
                            "GROUP BY text HAVING n > 1", (since,)).fetchall()
    return {r["text"] for r in rows}


def _clean(text: str) -> str:
    return " ".join(_URL.sub("", html.unescape(text or "")).split()).strip()


def outline(description: str, title: str = "", max_lines: int = 8) -> list[str]:
    """Chapters / bullets / summary sentences from a YouTube description, minus boilerplate.
    Stops at the first separator line (what follows is nearly always sponsor links)."""
    chapters, bullets, prose = [], [], []
    for raw in (description or "").splitlines():
        line = raw.strip()
        if _SEPARATOR.match(line):
            break
        if not line or _PROMO.search(line) or (title and _clean(line).lower() == title.lower()):
            continue
        text = _clean(line)
        if len(text) < 4 or text.endswith(":") and len(text) < 40:
            continue
        m = _CHAPTER.match(text)
        if m:
            title = m.group("title").strip()
            if not re.fullmatch(r"(intro(duction)?|outro|start|end|conclusion)", title, re.I):
                chapters.append(title)
            continue
        m = _BULLET.match(line)
        if m:
            bullets.append(_clean(m.group(1)))
            continue
        if not _URL.search(line) and len(text) >= 25 and len(prose) < 2 \
                and not (title and text.lower().startswith(title.lower()[:30])):
            prose.append(text[:220])
    return (prose + chapters + bullets)[:max_lines]


def _near_dupe(text: str, seen: list[str]) -> bool:
    key = re.sub(r"\W+", " ", text.lower())[:80]
    if key in seen:
        return True
    seen.append(key)
    return False


def _stance(sentiment: float) -> str:
    return "🟢" if sentiment > 0.15 else "🔴" if sentiment < -0.1 else "⚪"


def gather(store, since: float, x_accounts: list[str], yt_channels: list[str], skip=frozenset()) -> dict:
    """{"x": {handle: [rows]}, "yt": {handle: [(video_row, post_row|None, [comments])]}}"""
    x_set = {f"x:@{a.strip().lstrip('@').lower()}" for a in x_accounts} - set(skip)
    yt_set = {h.strip().lstrip("@").lower() for h in yt_channels}
    db = store.db
    x_posts: dict[str, list] = defaultdict(list)
    for r in db.execute("SELECT * FROM posts WHERE source = 'x' AND created_utc >= ? ORDER BY created_utc DESC",
                        (since,)):
        if r["channel"].lower() in x_set:
            x_posts[r["channel"][3:]].append(r)
    videos = db.execute("SELECT * FROM youtube_videos WHERE published_utc >= ? ORDER BY published_utc DESC",
                        (since,)).fetchall()
    comments: dict[str, list] = defaultdict(list)
    for r in db.execute("SELECT * FROM posts WHERE id LIKE 'yt:c:%' AND created_utc >= ?", (since - 2 * 86400,)):
        m = re.search(r"[?&]v=([\w-]+)", r["url"] or "")
        if m:
            comments[m.group(1)].append(r)
    yt: dict[str, list] = defaultdict(list)
    for v in videos:
        if v["handle"].lower() not in yt_set:
            continue
        post = db.execute("SELECT * FROM posts WHERE id = ?", (f"yt:{v['video_id']}",)).fetchone()
        yt[v["handle"]].append((v, post, comments.get(v["video_id"], [])))
    return {"x": x_posts, "yt": yt}


def coins_by_post(store, post_ids: list[str]) -> dict[str, list[str]]:
    if not post_ids:
        return {}
    out: dict[str, list[str]] = defaultdict(list)
    q = ",".join("?" * len(post_ids))
    for r in store.db.execute(
            f"""SELECT m.post_id, COALESCE(c.symbol, m.coin_key) AS sym FROM mentions m
                LEFT JOIN coins c ON c.key = m.coin_key WHERE m.post_id IN ({q})""", post_ids):
        sym = (r["sym"] or "").upper().lstrip("$")
        if sym and sym not in out[r["post_id"]]:
            out[r["post_id"]].append(sym)
    return out


def render(store, cfg: dict, hours: float = 24, now: float | None = None, account: str | None = None,
           max_x_posts: int = 6, max_videos: int = 4, max_comments: int = 3) -> str:
    now = now or time.time()
    since = now - hours * 3600
    skip = {f"x:@{a.lower()}" for a in cfg.get("brand_accounts", []) + cfg.get("x_signal_accounts", [])}
    data = gather(store, since, cfg.get("x_accounts", []), cfg.get("youtube_channels", []), skip)
    shills = shill_words(store, since - 6 * 86400)
    pasted = copy_paste(store, since - 6 * 86400)
    want = account.strip().lstrip("@").lower() if account else None
    lines = [f"What your accounts are saying (last {hours:g}h)", ""]

    x_items = sorted(data["x"].items(), key=lambda kv: -len(kv[1]))
    if want:
        x_items = [kv for kv in x_items if want in kv[0].lower()]
    coin_map = coins_by_post(store, [r["id"] for _, rows in x_items for r in rows])
    if x_items:
        lines.append("𝕏 X")
    for handle, rows in x_items:
        bull = sum(r["sentiment"] > 0.15 for r in rows)
        bear = sum(r["sentiment"] < -0.1 for r in rows)
        coins = sorted({c for r in rows for c in coin_map.get(r["id"], [])})
        head = f"@{handle}: {len(rows)} posts ({bull} bullish / {bear} bearish)"
        if coins:
            head += " · coins: " + ", ".join(coins[:10])
        lines.append(head)
        seen: list[str] = []
        shown = 0
        for r in rows:
            text = _clean(r["text"])
            if len(text) < 12 or _near_dupe(text, seen):
                continue
            when = time.strftime("%a %H:%M", time.gmtime(r["created_utc"]))
            lines.append(f"  {_stance(r['sentiment'])} {when}: {text[:280]}")
            shown += 1
            if shown >= max_x_posts:
                break
        if not shown:
            lines.append("  (links / images only)")
        lines.append("")

    yt_items = sorted(data["yt"].items(), key=lambda kv: -max((v["vs_norm"] or 0) for v, _, _ in kv[1]))
    if want:
        yt_items = [kv for kv in yt_items if want in kv[0].lower()]
    if yt_items:
        lines.append("▶ YOUTUBE")
    for handle, vids in yt_items:
        lines.append(f"@{handle}: {len(vids)} new video{'s' if len(vids) != 1 else ''}")
        for v, post, comments in sorted(vids, key=lambda t: -(t[0]["vs_norm"] or 0))[:max_videos]:
            pace = f", {v['vs_norm']:.1f}x their usual" if v["vs_norm"] else ""
            when = time.strftime("%a %H:%M", time.gmtime(v["published_utc"]))
            lines.append(f"  • \"{v['title']}\" ({when}, {v['views']:,} views{pace}, {v['comments']:,} comments)")
            desc = (post["text"].split("\n", 1)[1] if post and "\n" in post["text"] else "")
            for item in outline(desc, v["title"]):
                lines.append(f"      – {item}")
            real = [c for c in comments if not _COMMENT_SPAM.search(c["text"] or "") and len(_clean(c["text"])) >= 20
                    and c["text"] not in pasted
                    and not ({w.lower() for w in _WORD.findall(c["text"] or "")} & shills.keys())]
            if real:
                mood = sum(c["sentiment"] for c in real) / len(real)
                word = "positive" if mood > 0.15 else "negative" if mood < -0.1 else "mixed"
                lines.append(f"      viewers ({len(real)} comments, {word}):")
                for c in sorted(real, key=lambda c: -len(c["text"]))[:max_comments]:
                    lines.append(f"        \"{_clean(c['text'])[:160]}\"")
        lines.append("")
    loud = [w for w, n in sorted(shills.items(), key=lambda kv: -kv[1]) if n >= 8]
    if loud:
        lines.append("Bot comment campaigns filtered out: " + ", ".join(loud[:8]))
    if not x_items and not yt_items:
        lines.append("  nothing from your accounts in this window")
    return "\n".join(lines).rstrip()
