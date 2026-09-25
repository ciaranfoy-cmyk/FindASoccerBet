"""Turn stored mentions into rankings: most talked about, heating up, and new on the radar.

Unit of buzz is a "voice": a distinct (author, hour) pair per coin. One account
spamming a ticker 50 times in an hour counts once, and 20 different people
mentioning it counts 20 times. Voices add up over time, so a 6-hour window can
be compared fairly against a 72-hour baseline.
"""

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .extract import STABLECOINS
from .store import Store

SOURCE_LETTER = {"reddit": "R", "telegram": "T", "4chan": "4", "news": "N", "x": "X"}


@dataclass
class CoinStats:
    key: str
    label: str = ""
    voices: int = 0            # distinct (author, hour) in window
    mentions: int = 0          # raw post count in window
    authors: set = field(default_factory=set)
    channels: dict = field(default_factory=lambda: defaultdict(int))
    sources: set = field(default_factory=set)
    sentiment_sum: float = 0.0
    base_voices: int = 0       # distinct (author, hour) in baseline
    velocity: float = 0.0      # window voices/hour ÷ baseline voices/hour
    heat: float = 0.0          # rising score
    is_new: bool = False
    info: dict = field(default_factory=dict)
    search: "SearchInfo | None" = None
    pump_only: bool = False    # every mention in the window came from signal/pump channels

    @property
    def sentiment(self) -> float:
        return self.sentiment_sum / self.mentions if self.mentions else 0.0

    @property
    def top_channels(self) -> list[str]:
        return [c for c, _ in sorted(self.channels.items(), key=lambda kv: -kv[1])[:3]]


@dataclass
class SearchInfo:
    cg_rank: int | None = None          # position on CoinGecko trending right now
    cg_since: float | None = None       # when its current streak on the list began
    cg_since_is_floor: bool = False     # streak goes back past our lookback
    google: list = field(default_factory=list)  # [(fetched_utc, source, detail)] in last 24h
    rank_before: int | None = None      # rank ~2h ago (None = wasn't on the list)
    price: float | None = None
    change_1h: float | None = None      # percent
    change_24h: float | None = None     # percent
    change_since_entry: float | None = None  # percent since its current streak began
    early: bool = False                 # climbing in searches and price hasn't run yet
    already_pumped: bool = False        # searched because it already moved

    def entered_since(self, t: float) -> bool:
        return self.cg_rank is not None and not self.cg_since_is_floor and self.cg_since >= t

    @property
    def climb(self) -> int:
        """Places gained in ~2h; entering the list counts as coming from #16."""
        if self.cg_rank is None:
            return 0
        return (self.rank_before or 16) - self.cg_rank

# A coin climbing the search list is "early" while its 24h move is below this;
# at or above it, the searches are most likely people reacting to the pump.
PUMPED_PCT = 15.0


def search_status(store: Store, now: float, lookback_h: float = 48,
                  stale_after_h: float = 2) -> dict[str, SearchInfo]:
    """Who is on CoinGecko trending now (and since when), and who hit Google Trends."""
    rows = store.search_rows(now - lookback_h * 3600)
    out: dict[str, SearchInfo] = defaultdict(SearchInfo)

    snapshots: dict[float, dict[str, int]] = defaultdict(dict)
    for r in rows:
        if r["source"] == "coingecko":
            snapshots[r["fetched_utc"]][r["coin_key"]] = r["rank"]
        elif r["fetched_utc"] >= now - 24 * 3600:
            out[r["coin_key"]].google.append((r["fetched_utc"], r["source"], r["detail"]))

    times = sorted(snapshots)
    if times and times[-1] >= now - stale_after_h * 3600:
        latest = snapshots[times[-1]]
        for key, rank in latest.items():
            since, floor = times[-1], True
            for t in reversed(times):
                if key not in snapshots[t]:
                    floor = False
                    break
                since = t
            info = out[key]
            info.cg_rank, info.cg_since, info.cg_since_is_floor = rank, since, floor
            earlier = [t for t in times if t <= times[-1] - 2 * 3600]
            info.rank_before = snapshots[earlier[-1]].get(key) if earlier else None

        prices: dict[str, list] = defaultdict(list)
        for r in store.price_rows(now - lookback_h * 3600):
            prices[r["coin_key"]].append(r)
        for key in latest:
            info, hist = out[key], prices.get(key)
            if not hist:
                continue
            last = hist[-1]
            info.price, info.change_1h, info.change_24h = last["price"], last["change_1h"], last["change_24h"]
            at_entry = next((r for r in hist if r["fetched_utc"] >= info.cg_since), None)
            if at_entry and at_entry["price"] and not info.cg_since_is_floor:
                info.change_since_entry = (last["price"] / at_entry["price"] - 1) * 100
            info.already_pumped = info.change_24h is not None and info.change_24h >= PUMPED_PCT
            fresh = info.entered_since(now - 3 * 3600)
            info.early = (info.change_24h is not None and not info.already_pumped
                          and info.cg_rank <= 10 and (fresh or info.climb >= 5))
    return dict(out)


@dataclass
class Report:
    window_h: float
    baseline_h: float
    history_h: float
    stats: list[CoinStats]
    now: float = 0.0

    def most_talked(self, n: int = 20) -> list[CoinStats]:
        talked = [s for s in self.stats if s.voices]
        return sorted(talked, key=lambda s: (-s.voices, -s.mentions))[:n]

    def search_signals(self) -> list[CoinStats]:
        """Climbing the search list while the price hasn't run yet."""
        return sorted((s for s in self.stats if s.search and s.search.early),
                      key=lambda s: s.search.cg_rank)

    def search_interest(self) -> list[CoinStats]:
        """Coins on CoinGecko trending now, then any that hit Google Trends in 24h."""
        on_cg = [s for s in self.stats if s.search and s.search.cg_rank is not None]
        on_google = [s for s in self.stats if s.search and s.search.google and s not in on_cg]
        return sorted(on_cg, key=lambda s: s.search.cg_rank) + on_google

    def heating_up(self, n: int = 15, min_voices: int = 3) -> list[CoinStats]:
        rising = [s for s in self.stats
                  if s.voices >= min_voices and s.velocity > 1.5 and not s.pump_only]
        return sorted(rising, key=lambda s: -s.heat)[:n]

    def new_on_radar(self, n: int = 15, min_voices: int = 2) -> list[CoinStats]:
        fresh = [s for s in self.stats if s.is_new and s.voices >= min_voices and not s.pump_only]
        return sorted(fresh, key=lambda s: -s.voices)[:n]

    def pump_only_coins(self) -> list[CoinStats]:
        return sorted((s for s in self.stats if s.pump_only), key=lambda s: -s.mentions)


def _label(key: str, info: dict) -> str:
    symbol, name = info.get("symbol"), info.get("name")
    if key.startswith("ca:"):
        if symbol:
            return f"${symbol} ({info.get('chain') or '?'} {key[3:9]}…)"
        return f"{key[3:9]}…{key[-4:]} (contract)"
    if key.startswith("$"):
        return f"{key} (unlisted)"
    return f"{symbol} · {name}" if name and name.upper() != symbol else (symbol or key)


def build(store: Store, window_h: float = 6, baseline_h: float = 72,
          now: float | None = None, pump_channels: set[str] | frozenset = frozenset()) -> Report:
    """pump_channels: channel names (e.g. "t.me/degenpump_crypto_pump_signals") whose posts
    are paid signals/pump calls. Coins only they mention are listed separately, not ranked."""
    now = now or time.time()
    window_start = now - window_h * 3600
    base_start = window_start - baseline_h * 3600

    stats: dict[str, CoinStats] = {}
    voices_now: dict[str, set] = defaultdict(set)
    voices_base: dict[str, set] = defaultdict(set)

    for row in store.mention_rows(base_start):
        key = row["coin_key"]
        hour = int(row["created_utc"] // 3600)
        voice = (row["author"], hour)
        if row["created_utc"] < window_start:
            voices_base[key].add(voice)
            continue
        s = stats.setdefault(key, CoinStats(key=key))
        voices_now[key].add(voice)
        s.mentions += 1
        s.authors.add(row["author"])
        s.channels[row["channel"]] += 1
        s.sources.add(row["source"])
        s.sentiment_sum += row["sentiment"]

    search = search_status(store, now)
    for key in search:
        stats.setdefault(key, CoinStats(key=key))  # searched-for but not (yet) talked about

    keys = list(stats)
    info = store.coin_info(keys)
    first_seen = store.first_seen(keys)
    oldest = store.oldest_post_utc() or now
    history_h = (now - oldest) / 3600
    # If we don't have a full baseline yet, measure the rate over what we do have.
    effective_base_h = max(min(baseline_h, history_h - window_h), 1.0)

    out = []
    for key, s in stats.items():
        row = info.get(key)
        s.info = dict(row) if row else {}
        if key.startswith("ca:") and s.info.get("resolved_utc") and not s.info.get("symbol"):
            continue  # DEX Screener checked it: not a traded token
        if (s.info.get("symbol") or "").upper() in STABLECOINS:
            continue  # stored before stablecoins were filtered at extraction
        s.label = _label(key, s.info)
        s.voices = len(voices_now[key])
        s.base_voices = len(voices_base[key])
        rate_now = s.voices / window_h
        rate_base = (s.base_voices + 1) / effective_base_h  # +1: smoothing for never-seen coins
        s.velocity = rate_now / rate_base
        s.heat = math.log2(max(s.velocity, 1.0)) * math.sqrt(s.voices) * (1 + 0.25 * (len(s.sources) - 1))
        s.is_new = first_seen.get(key, 0) >= window_start and history_h > window_h * 2
        s.pump_only = bool(s.channels) and all(c in pump_channels for c in s.channels)
        s.search = search.get(key)
        if s.search:
            # Search interest confirming social buzz is a stronger signal than either alone.
            if s.search.entered_since(window_start):
                s.heat *= 1.5
            if s.search.google:
                s.heat *= 2
        out.append(s)

    return Report(window_h=window_h, baseline_h=baseline_h, history_h=history_h, stats=out,
                  now=now)


def _sentiment_bar(x: float) -> str:
    if x > 0.35:
        return "++"
    if x > 0.1:
        return "+ "
    if x < -0.35:
        return "--"
    if x < -0.1:
        return "- "
    return "~ "


def _money(x) -> str:
    if x is None:
        return "?"
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:.1f}{unit}"
    return f"${x:.0f}"


def _search_tags(s: CoinStats, window_start: float) -> list[str]:
    tags = []
    if s.search and s.search.cg_rank is not None:
        tags.append(f"CG#{s.search.cg_rank}" + ("↑new" if s.search.entered_since(window_start) else ""))
    if s.search and s.search.google:
        tags.append("GOOGLE-TRENDING")
    if s.search and s.search.early:
        tags.append("EARLY?")
    return tags


def _row(s: CoinStats, window_start: float) -> str:
    srcs = "".join(SOURCE_LETTER[k] for k in SOURCE_LETTER if k in s.sources)
    extra = _search_tags(s, window_start) + (["[pump/signal only]"] if s.pump_only else [])
    if s.key.startswith("ca:") and s.info.get("symbol"):
        extra.append(f"liq {_money(s.info.get('liquidity_usd'))}")
        if s.info.get("price_change_24h") is not None:
            extra.append(f"24h {s.info['price_change_24h']:+.0f}%")
    vel = f"{s.velocity:5.1f}x" if s.base_voices else "  new "
    return (f"{s.label[:34]:<34} {s.voices:>4} {s.mentions:>5}  {vel}  "
            f"{_sentiment_bar(s.sentiment)} {s.sentiment:+.2f}  {srcs:<5} "
            f"{', '.join(s.top_channels)[:40]:<40} {' '.join(extra)}")


HEADER = (f"{'coin':<34} {'voic':>4} {'posts':>5}  {'vs base':>6}  {'sentiment':<9}  "
          f"{'src':<5} {'top channels':<40}")


def render_text(report: Report, top: int = 20) -> str:
    lines = [f"Window: last {report.window_h:g}h vs previous {report.baseline_h:g}h   "
             f"(history in DB: {report.history_h:.1f}h)"]
    if report.history_h < report.window_h + report.baseline_h:
        lines.append("Note: baseline is still filling up; 'vs base' and 'new' get more reliable "
                     "after a few days of collecting.")
    lines.append("Sources: R=Reddit T=Telegram 4=4chan N=News X=X/Twitter   "
                 "voic = distinct people-hours talking about it\n")

    sections = [
        ("MOST TALKED ABOUT", report.most_talked(top)),
        ("HEATING UP (buzz rising fastest vs its own baseline)", report.heating_up(top)),
        ("NEW ON THE RADAR (first mentions ever, in this window)", report.new_on_radar(top)),
    ]
    window_start = report.now - report.window_h * 3600
    for title, rows in sections:
        lines.append(f"== {title} ==")
        if rows:
            lines.append(HEADER)
            lines.extend(_row(s, window_start) for s in rows)
        else:
            lines.append("  (nothing yet)")
        lines.append("")

    pumped = report.pump_only_coins()
    if pumped:
        lines.append("== ONLY IN SIGNAL/PUMP CHANNELS (not ranked above) ==")
        lines.append("  " + ", ".join(f"{s.label.split(' · ')[0]} ({s.mentions})" for s in pumped[:40]))
        lines.append("")

    signals = report.search_signals()
    lines.append("== EARLY SEARCH SIGNALS (climbing CoinGecko searches, price not run yet) ==")
    if signals:
        for s in signals:
            si = s.search
            came = f"from #{si.rank_before}" if si.rank_before else "new to list"
            lines.append(f"{s.label[:28]:<28} #{si.cg_rank} ({came})  price 24h {si.change_24h:+.0f}%"
                         f"  chatter: {s.voices} voices")
    else:
        lines.append("  (none right now)")
    lines.append("")

    lines.append("== SEARCH INTEREST (CoinGecko trending now + Google Trends, 24h) ==")
    searched = report.search_interest()
    if not searched:
        lines.append("  (no search data yet)")
    for s in searched:
        si = s.search
        if si.cg_rank is not None:
            hours = (report.now - si.cg_since) / 3600
            since = f"{hours:.0f}h+" if si.cg_since_is_floor else f"{hours:.1f}h"
            where = f"CoinGecko #{si.cg_rank:<2} on list {since:>6}"
        else:
            where = f"{'':<28}"
        move = ""
        if si.rank_before is not None and si.cg_rank is not None and si.rank_before != si.cg_rank:
            move = f" (was #{si.rank_before} 2h ago)"
        elif si.cg_rank is not None and si.rank_before is None and si.entered_since(report.now - 2 * 3600):
            move = " (new in 2h)"
        price = ""
        if si.change_24h is not None:
            price = f"  price 24h {si.change_24h:+.0f}%"
            if si.change_1h is not None:
                price += f", 1h {si.change_1h:+.1f}%"
            if si.change_since_entry is not None:
                price += f", since listed {si.change_since_entry:+.0f}%"
        flag = "  <- EARLY? climbing, price not run yet" if si.early else (
            "  (searched after a pump)" if si.already_pumped else "")
        buzz = f"{s.voices} voices" + (f", {s.velocity:.1f}x usual" if s.voices else " (no chatter)")
        google = "  GOOGLE: " + "; ".join(d for _, _, d in si.google[-2:]) if si.google else ""
        lines.append(f"{s.label[:28]:<28} {where}{move}  {buzz}{price}{flag}{google}")
    lines.append("")
    return "\n".join(lines)


def render_search_alert(s: CoinStats) -> str:
    si = s.search
    came = f"from #{si.rank_before}" if si.rank_before else "new to the list"
    parts = [f"🔎 {s.label} climbing in searches",
             f"#{si.cg_rank} on CoinGecko trending ({came}, ~2h)",
             f"price 24h {si.change_24h:+.1f}%" + (f", 1h {si.change_1h:+.1f}%" if si.change_1h is not None else ""),
             f"chatter: {s.voices} voices" + (f" in {', '.join(sorted(s.sources))}" if s.sources else " (none in our sources)"),
             "Search-only interest can be a pump organised elsewhere: look, don't leap."]
    return "\n".join(parts)


def render_alert(s: CoinStats) -> str:
    """Short plain-text message for a Telegram alert."""
    parts = [f"🔥 {s.label} heating up",
             f"{s.voices} voices in window, {s.velocity:.1f}x its usual rate",
             f"sentiment {s.sentiment:+.2f}, sources: {', '.join(sorted(s.sources))}",
             f"where: {', '.join(s.top_channels)}"]
    if s.search and s.search.cg_rank is not None:
        parts.append(f"searches: #{s.search.cg_rank} on CoinGecko trending")
    if s.search and s.search.google:
        parts.append(f"on Google Trends: {s.search.google[-1][2]}")
    if s.info.get("url"):
        parts.append(f"liq {_money(s.info.get('liquidity_usd'))} · {s.info['url']}")
    return "\n".join(parts)
