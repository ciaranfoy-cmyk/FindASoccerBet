"""The digest: what people are talking about, today and over the week.

No calls or scores — just who is discussing which coins, where, how that is shifting
week over week, and what search, LunarCrush and news say alongside it."""

from __future__ import annotations

import time
from collections import defaultdict

from .report import Report, SOURCE_LETTER, _label
from .signals import _fmt_price, _market_line

WEEK = 7 * 86400
MIN_WEEK_VOICES = 6          # a coin needs this many people-hours in a week to count as a mover
PUMPED_PCT = 15.0
SOURCE_NAME = {"reddit": "Reddit", "telegram": "Telegram", "4chan": "4chan", "news": "news",
               "x": "X", "youtube": "YouTube"}


def weekly_voices(store, now: float, pump_channels=frozenset(), hidden=frozenset()) -> tuple[dict, dict]:
    """People-hours talking about each coin this week and the week before (pump channels,
    YouTube comments and hidden market coins left out)."""
    this_week: dict[str, set] = defaultdict(set)
    last_week: dict[str, set] = defaultdict(set)
    for row in store.mention_rows(now - 2 * WEEK):
        key = row["coin_key"]
        if key in hidden or row["channel"] in pump_channels or row["id"].startswith("yt:c:"):
            continue
        voice = (row["author"], int(row["created_utc"] // 3600))
        (this_week if row["created_utc"] >= now - WEEK else last_week)[key].add(voice)
    return ({k: len(v) for k, v in this_week.items()}, {k: len(v) for k, v in last_week.items()})


def share_shifts(this_week: dict, last_week: dict, min_voices: int = MIN_WEEK_VOICES):
    """[(key, share_now %, share_before %, voices_now, voices_before)], biggest gain first."""
    total_now, total_before = sum(this_week.values()) or 1, sum(last_week.values()) or 1
    rows = []
    for key in set(this_week) | set(last_week):
        now_v, before_v = this_week.get(key, 0), last_week.get(key, 0)
        if max(now_v, before_v) < min_voices:
            continue
        rows.append((key, 100 * now_v / total_now, 100 * before_v / total_before, now_v, before_v))
    return sorted(rows, key=lambda r: -(r[1] - r[2]))


def _prices(store, keys: list[str], now: float) -> dict:
    """{key: (price, change_24h, change_7d)} for the coins we have prices for."""
    latest = store.latest_prices(keys, now - 6 * 3600)
    out = {}
    for key, r in latest.items():
        week_ago = store.price_near(key, now - WEEK, tolerance_s=12 * 3600)
        ch7 = (r["price"] / week_ago - 1) * 100 if r["price"] and week_ago else None
        out[key] = (r["price"], r["change_24h"], ch7)
    return out


def _price_bit(p) -> str:
    if not p or not p[0]:
        return ""
    price, ch24, ch7 = p
    bits = [_fmt_price(price)]
    if ch24 is not None:
        bits.append(f"{ch24:+.0f}% 24h")
    if ch7 is not None:
        bits.append(f"{ch7:+.0f}% 7d")
    return " · " + ", ".join(bits)


def _mood(s) -> str:
    x = s.sentiment
    return "very positive" if x > 0.35 else "positive" if x > 0.1 else "negative" if x < -0.1 else "mixed"


def _short(label: str) -> str:
    return label.split(" ·")[0]


def render(rep: Report, store, now: float | None = None, pump_channels=frozenset(),
           top: int = 8) -> str:
    """`rep` should be a 24h-window report with BTC/ETH set as hidden."""
    now = now or time.time()
    labels = {s.key: s.label for s in rep.stats}
    lines = [_market_line(rep, store, now), ""]

    this_week, last_week = weekly_voices(store, now, pump_channels, rep.hidden)
    shifts = share_shifts(this_week, last_week)
    talked = [s for s in rep.most_talked(40) if not s.pump_only][:top]
    keys = list({s.key for s in talked} | {r[0] for r in shifts[:6]} | {r[0] for r in shifts[-4:]})
    prices = _prices(store, keys, now)
    missing = [k for k in keys if k not in labels]
    labels.update({k: _label(k, dict(row)) for k, row in store.coin_info(missing).items()})

    lines.append(f"🗣 MOST TALKED ABOUT (last {rep.window_h:g}h, BTC/ETH aside)")
    for s in talked:
        where = ", ".join(SOURCE_NAME.get(src, src) for src in sorted(s.sources, key=lambda x: SOURCE_LETTER.get(x, x)))
        usual = f", {s.velocity:.1f}x usual" if s.base_voices else ", new"
        lines.append(f"{s.label}: {s.voices} people{usual} ({where}; {_mood(s)}){_price_bit(prices.get(s.key))}")
    if not talked:
        lines.append("  quiet: no coin is being discussed much")
    lines.append("")

    if last_week:
        gaining = [r for r in shifts if r[1] > r[2]][:6]
        fading = [r for r in reversed(shifts) if r[1] < r[2]][:4]
        lines.append("📈 GAINING ATTENTION THIS WEEK (share of all coin talk, vs the week before)")
        for key, now_s, before_s, *_ in gaining:
            lines.append(f"{labels.get(key, key)}: {before_s:.1f}% → {now_s:.1f}%{_price_bit(prices.get(key))}")
        if not gaining:
            lines.append("  nothing gaining much")
        if fading:
            lines.append("📉 Fading: " + ", ".join(
                f"{_short(labels.get(k, k))} {b:.1f}% → {n:.1f}%" for k, n, b, *_ in fading))
        lines.append("")

    voiced = sorted((s for s in rep.ranked if s.experts), key=lambda s: -len(s.experts))[:8]
    if voiced or rep.videos:
        lines.append("🎙 YOUR ACCOUNTS & YOUTUBERS")
        for s in voiced:
            names = ", ".join(sorted(e.split(":", 1)[1] for e in s.experts))
            lines.append(f"{_short(s.label)}: {names}")
        vids = sorted(rep.videos, key=lambda v: -(v["vs_norm"] or 0))[:3]
        for v in vids:
            pace = f", {v['vs_norm']:.1f}x their usual" if v["vs_norm"] else ""
            lines.append(f"▶ @{v['handle']}: \"{v['title'][:70]}\" ({v['views']:,} views{pace})")
        lines.append("")

    lunar = [s for s in rep.ranked if s.search and s.search.lunar and s.search.lunar.get("interactions_24h")]
    if lunar:
        biggest = sorted(lunar, key=lambda s: -s.search.lunar["interactions_24h"])[:6]
        rising = sorted((s for s in lunar if (s.search.lunar_growth or 0) >= 2
                         and s.search.lunar["interactions_24h"] >= 50_000 and not s.key.startswith("$")),
                        key=lambda s: -s.search.lunar_growth)[:5]
        lines.append("🌐 ACROSS SOCIAL MEDIA (LunarCrush, 24h)")
        lines.append("Most discussed: " + ", ".join(
            f"{_short(s.label)} {s.search.lunar['interactions_24h'] / 1e6:.1f}M" for s in biggest))
        if rising:
            lines.append("Rising fastest: " + ", ".join(
                f"{_short(s.label)} {s.search.lunar_growth:.0f}x" for s in rising))
        lines.append("")

    searched = [s for s in rep.ranked if s.search and s.search.cg_rank is not None]
    if searched:
        searched.sort(key=lambda s: s.search.cg_rank)
        lines.append("🔎 MOST SEARCHED (CoinGecko): " + ", ".join(
            f"{_short(s.label)}" + (f" {s.search.change_24h:+.0f}%" if s.search.change_24h is not None else "")
            for s in searched[:8]))
        lines.append("")

    seen, news = set(), []
    for s in rep.ranked:
        for created, headline, url, sent, n_coins in s.news:
            if n_coins <= 2 and headline[:60] not in seen:
                seen.add(headline[:60])
                news.append((created, _short(s.label), headline))
    if news:
        lines.append("📰 IN THE NEWS")
        for _, coin, headline in sorted(news, reverse=True)[:5]:
            lines.append(f"{coin}: {headline[:130]}")
        lines.append("")

    moved = [s for s in rep.ranked if s.search and s.search.change_24h is not None
             and s.search.change_24h >= PUMPED_PCT and not s.key.startswith("$")]
    if moved:
        lines.append("Big moves (24h): " + ", ".join(
            f"{_short(s.label)} {s.search.change_24h:+.0f}%" for s in sorted(moved, key=lambda s: -s.search.change_24h)[:6]))
    pushed = rep.pump_only_coins()[:6]
    if pushed:
        lines.append("Only in pump/signal channels (ignore): " + ", ".join(_short(s.label) for s in pushed))
    return "\n".join(lines).rstrip()
