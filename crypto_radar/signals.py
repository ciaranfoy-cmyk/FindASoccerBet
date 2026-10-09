"""Turn the report into calls: one score per coin from independent evidence, a bucket,
and the reasons, plus a track record of past calls.

Buckets
  green   early & confirmed: several independent signals agree, price hasn't run yet
  yellow  watch: one or two signals, needs confirmation
  red     late: already moved (15%+ in 24h, or 10%+ in an hour)
  black   likely pushed: attention without real people (pump channels only, search/trends
          with no chatter, shill comments, tiny liquidity)
"""

import re
import time
from dataclasses import dataclass, field

from .report import MEGA_CAP_USD, MOVING_1H_PCT, PUMPED_PCT, Report

GREEN_SCORE = 6
FALLING_PCT = -8.0         # 24h drop beyond which attention isn't "early"
_BAD_WORDS = re.compile(r"\b(hack(ed)?|exploit(ed)?|breach|drain(ed)?|stolen|theft|scam|rug|"
                        r"lawsuit|sued|charged|delist(ed|ing)?|halt(ed)?|insolven\w*|bankrupt\w*|"
                        r"security incident|loss(es)?|outage|vulnerab\w*)\b", re.I)
YELLOW_SCORE = 3
GREEN_KINDS = 3            # distinct kinds of evidence needed for green
LOG_COOLDOWN_H = 24        # don't re-log the same coin in the same bucket within this
OUTCOME_DAYS = (1, 3, 7)
ICON = {"green": "🟢", "yellow": "🟡", "red": "🔴", "black": "⚫"}


@dataclass
class Signal:
    key: str
    label: str
    bucket: str = ""
    score: float = 0.0
    kinds: set = field(default_factory=set)
    why: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    price: float | None = None
    change_24h: float | None = None
    change_1h: float | None = None
    market_cap: float | None = None
    voices: int = 0
    confirm: str = ""
    invalidate: str = ""


def _bad_news(n) -> bool:
    """n = (created_utc, headline, url, sentiment)."""
    return bool(_BAD_WORDS.search(n[1])) or (len(n) > 3 and n[3] is not None and n[3] < -0.3)


def _price_info(s, latest_prices) -> tuple:
    """(price, change_24h, change_1h, market_cap) from the best source we have."""
    si = s.search
    if si and si.price is not None:
        return si.price, si.change_24h, si.change_1h, si.market_cap
    row = latest_prices.get(s.key)
    if row is not None:
        return row["price"], row["change_24h"], row["change_1h"], row["market_cap"]
    if si and si.lunar.get("change_24h") is not None:
        return None, si.lunar["change_24h"], None, None
    return None, None, None, None


def _fmt_price(p: float | None) -> str:
    if p is None:
        return "?"
    return f"${p:,.0f}" if p >= 100 else f"${p:,.2f}" if p >= 1 else f"${p:.4g}"


def score(s, latest_prices: dict) -> Signal:
    si = s.search
    sig = Signal(key=s.key, label=s.label, voices=s.voices)
    sig.price, sig.change_24h, sig.change_1h, sig.market_cap = _price_info(s, latest_prices)
    real_sources = {src for src in s.sources} - ({"telegram"} if s.pump_only else set())

    # --- evidence -------------------------------------------------------------------
    if s.voices >= 3 and s.velocity >= 2 and not s.pump_only:
        sig.score += 2
        sig.kinds.add("chatter")
        sig.why.append(f"chatter {s.velocity:.1f}x normal ({s.voices} people, {', '.join(sorted(real_sources))})")
    elif s.voices >= 5 and s.velocity >= 1.3 and not s.pump_only:
        sig.score += 1
        sig.kinds.add("chatter")
        sig.why.append(f"chatter up {s.velocity:.1f}x ({s.voices} people)")
    if len(real_sources) >= 3 and not s.pump_only:
        sig.score += 1
        sig.why.append(f"talked about on {len(real_sources)} platforms")
    if s.experts:
        sig.score += 2 + (1 if len(s.experts) >= 2 else 0)
        sig.kinds.add("experts")
        names = ", ".join(sorted(e.split(":", 1)[1] for e in s.experts)[:4])
        sig.why.append(f"your accounts: {names}")
    # Good news must be about this coin (<=2 coins named); bad news counts even when it
    # names a few (a hack hitting several chains still hits this one).
    good_news = [n for n in s.news if not _bad_news(n) and (len(n) < 5 or n[4] <= 2)]
    bad_news = [n for n in s.news if _bad_news(n)]
    if good_news:
        sig.score += 1
        sig.kinds.add("news")
        sig.why.append(f"news: {sorted(good_news)[-1][1][:110]}")
    if si:
        if si.cg_rank is not None and (si.climb >= 3 or si.rank_before is None):
            sig.score += 2
            sig.kinds.add("search")
            came = f"from #{si.rank_before}" if si.rank_before else "new"
            sig.why.append(f"CoinGecko searches #{si.cg_rank} ({came})")
        elif si.cg_rank is not None:
            sig.score += 1
            sig.kinds.add("search")
            sig.why.append(f"CoinGecko searches #{si.cg_rank}")
        if si.google:
            sig.score += 1
            sig.kinds.add("trends")
            src, detail = si.google[-1][1], si.google[-1][2]
            sig.why.append(f"{'X' if src.startswith('x-') else 'Google'} trending: {detail}")
        if si.lunar_growth and si.lunar_growth >= 1.5 and (si.lunar.get("interactions_24h") or 0) >= 50_000:
            sig.score += 2
            sig.kinds.add("lunar")
            sig.why.append(f"LunarCrush social activity {si.lunar_growth:.1f}x vs {si.lunar_growth_h:.0f}h ago")
    if s.sentiment > 0.15 and s.mentions >= 3:
        sig.score += 0.5

    # --- risks ----------------------------------------------------------------------
    chg, chg1 = sig.change_24h, sig.change_1h
    late = (chg is not None and chg >= PUMPED_PCT) or (chg1 is not None and chg1 >= MOVING_1H_PCT)
    if chg is not None and chg >= PUMPED_PCT:
        sig.risks.append(f"already up {chg:.0f}% in 24h")
    if chg1 is not None and abs(chg1) >= MOVING_1H_PCT:
        sig.risks.append(f"moving {chg1:+.0f}% in the last hour")
    if bad_news:
        sig.risks.append(f"bad news: {sorted(bad_news)[-1][1][:100]}")
    falling = chg is not None and chg <= FALLING_PCT
    if falling:
        sig.risks.append(f"price falling ({chg:.0f}% 24h): attention may be people reacting to the drop")
    liq = s.info.get("liquidity_usd")
    tiny_liq = s.key.startswith("ca:") and liq is not None and liq < 100_000
    if tiny_liq:
        sig.risks.append(f"only ${liq:,.0f} liquidity")
    attention = si and (si.cg_rank is not None or si.google or si.lunar_rank)
    pushed = (s.pump_only
              or (attention and s.voices == 0 and not s.experts and not s.news
                  and (sig.market_cap or 0) < 100e6 and si and (si.cg_rank or 99) <= 5)
              or (s.key.startswith("$") and not s.experts and not s.news and s.voices < 3))
    if s.pump_only:
        sig.risks.append("only pump/signal channels or shill comments mention it")
    elif pushed and attention and s.voices == 0:
        sig.risks.append("searched/trending but nobody real is talking about it")
    if (sig.market_cap or 0) >= MEGA_CAP_USD:
        sig.score = min(sig.score, GREEN_SCORE - 1)    # mega caps: context, not calls

    # --- bucket ---------------------------------------------------------------------
    if pushed or tiny_liq:
        sig.bucket = "black"
    elif late:
        sig.bucket = "red"
    elif sig.score >= GREEN_SCORE and len(sig.kinds) >= GREEN_KINDS and not falling and not bad_news:
        sig.bucket = "green"
    elif sig.score >= YELLOW_SCORE:
        sig.bucket = "yellow"

    if sig.bucket in ("green", "yellow"):
        p = _fmt_price(sig.price)
        sig.confirm = (f"more of your accounts/YouTubers pick it up and price holds above {p}"
                       if sig.price else "more independent sources pick it up")
        floor = _fmt_price(sig.price * 0.9) if sig.price else None
        sig.invalidate = (f"chatter fades, or price falls below {floor} (-10%)" if floor
                          else "chatter fades back to normal")
    return sig


def build_signals(rep: Report, store, now: float | None = None) -> list[Signal]:
    now = now or time.time()
    keys = [s.key for s in rep.ranked]
    latest = store.latest_prices(keys, now - 3 * 3600)
    out = [score(s, latest) for s in rep.ranked]
    out = [g for g in out if g.bucket]
    order = {"green": 0, "yellow": 1, "red": 2, "black": 3}
    return sorted(out, key=lambda g: (order[g.bucket], -g.score))


def price_candidates(rep: Report, registry_ids: set, extra: list[str] = ()) -> list[str]:
    """CoinGecko ids worth a price check: coins with some signal, plus open calls."""
    ids = set(extra) | {"bitcoin", "ethereum"}
    for s in sorted(rep.ranked, key=lambda s: -(s.voices + 3 * len(s.experts))):
        if s.key in registry_ids and (s.voices >= 2 or s.experts or s.news):
            ids.add(s.key)
        if len(ids) >= 120:
            break
    return sorted(ids)


# --- track record --------------------------------------------------------------------

def record(store, signals: list[Signal], now: float | None = None) -> list[Signal]:
    """Log green/yellow calls (once per coin+bucket per day; an upgrade is always logged).
    Returns the calls that are newly green (for instant alerts)."""
    now = now or time.time()
    fresh_green = []
    for g in signals:
        if g.bucket not in ("green", "yellow"):
            continue
        last = store.last_signal(g.key)
        if last and now - last["logged_utc"] < LOG_COOLDOWN_H * 3600 and \
                not (g.bucket == "green" and last["bucket"] == "yellow"):
            continue
        store.log_signal(g.key, g.label, g.bucket, g.score, now, g.price, " | ".join(g.why))
        if g.bucket == "green":
            fresh_green.append(g)
    store.commit()
    return fresh_green


def open_call_ids(store, registry_ids: set, now: float) -> list[str]:
    return sorted({r["coin_key"] for r in store.signals_since(now - (max(OUTCOME_DAYS) + 1) * 86400)
                   if r["coin_key"] in registry_ids})


TREND_PCT = 3.0   # price move over the 24h before a call that counts as "falling"/"rising"


def _outcomes(store, now: float):
    """Yields (row, days, return_pct, btc_return_pct or None) for calls old enough to judge."""
    for r in store.signals_since(now - 21 * 86400):
        if not r["price"]:
            continue
        for days in OUTCOME_DAYS:
            t = r["logged_utc"] + days * 86400
            if t > now:
                continue
            later = store.price_near(r["coin_key"], t)
            if later is None:
                continue
            b0, b1 = store.price_near("bitcoin", r["logged_utc"]), store.price_near("bitcoin", t)
            btc = (b1 / b0 - 1) * 100 if b0 and b1 else None
            yield r, days, (later / r["price"] - 1) * 100, btc


def scorecard(store, now: float | None = None) -> dict:
    """{bucket: {days: (n_checked, n_up, avg_return_pct, avg_vs_btc_pct|None, n_beat_btc)}}."""
    now = now or time.time()
    acc: dict = {}
    for r, days, ret, btc in _outcomes(store, now):
        a = acc.setdefault(r["bucket"], {}).setdefault(days, [0, 0, 0.0, 0, 0.0, 0])
        a[0] += 1
        a[1] += ret > 0
        a[2] += ret
        if btc is not None:
            a[3] += 1
            a[4] += ret - btc
            a[5] += ret > btc
    return {b: {d: (n, up, tot / n, (ex / nb) if nb else None, beat)
                for d, (n, up, tot, nb, ex, beat) in v.items()} for b, v in acc.items()}


def trend_split(store, now: float | None = None, min_n: int = 10) -> tuple[int, dict] | None:
    """Do calls made while the coin was already falling do worse than the rest?
    Returns (days, {"falling"|"flat"|"rising": (n, avg_vs_btc_pct)}) for the longest
    horizon with at least `min_n` judged calls, else None."""
    now = now or time.time()
    by_days: dict = {}
    for r, days, ret, btc in _outcomes(store, now):
        if btc is None or r["coin_key"] == "bitcoin":
            continue
        before = store.price_near(r["coin_key"], r["logged_utc"] - 86400)
        if not before:
            continue
        move = (r["price"] / before - 1) * 100
        kind = "falling" if move < -TREND_PCT else "rising" if move > TREND_PCT else "flat"
        by_days.setdefault(days, {}).setdefault(kind, []).append(ret - btc)
    for days in sorted(by_days, reverse=True):
        groups = by_days[days]
        if sum(len(v) for v in groups.values()) >= min_n:
            return days, {k: (len(v), sum(v) / len(v)) for k, v in groups.items()}
    return None


def render_scorecard(card: dict, split: tuple[int, dict] | None = None) -> str:
    parts = []
    for bucket in ("green", "yellow"):
        for days, (n, up, avg, vs_btc, beat) in sorted(card.get(bucket, {}).items()):
            bit = f"{ICON[bucket]} {days}d: {up}/{n} up, avg {avg:+.1f}%"
            if vs_btc is not None:
                bit += f" ({vs_btc:+.1f}% vs BTC, {beat} beat it)"
            parts.append(bit)
    out = "Track record (vs just holding BTC): " + ("; ".join(parts) if parts else "building (needs a day of calls + prices)")
    if split:
        days, groups = split
        out += (f"\nCalled while price was falling / flat / rising ({days}d, vs BTC): " + " · ".join(
            f"{k} {groups[k][1]:+.1f}% ({groups[k][0]})" for k in ("falling", "flat", "rising") if k in groups))
    return out


# --- digest --------------------------------------------------------------------------

def _market_line(rep: Report, store, now: float) -> str:
    latest = store.latest_prices(["bitcoin", "ethereum"], now - 6 * 3600)
    bits = []
    for key, name in (("bitcoin", "BTC"), ("ethereum", "ETH")):
        r = latest.get(key)
        if r is not None and r["price"]:
            ch = f" {r['change_24h']:+.1f}%" if r["change_24h"] is not None else ""
            bits.append(f"{name} {_fmt_price(r['price'])}{ch}")
    total = sum(s.mentions for s in rep.stats if not s.pump_only)
    mood = (sum(s.sentiment_sum for s in rep.stats if not s.pump_only) / total) if total else 0
    word = "upbeat" if mood > 0.15 else "cautious" if mood > 0.03 else "nervous" if mood > -0.05 else "fearful"
    rows = store.db.execute(
        """SELECT sentiment FROM posts WHERE created_utc >= ? AND id NOT LIKE 'yt:c:%'
           AND (channel LIKE 'x:@%' OR channel LIKE 'yt:@%')""", (now - 12 * 3600,)).fetchall()
    bull = sum(1 for r in rows if r[0] > 0.15)
    bear = sum(1 for r in rows if r[0] < -0.1)
    experts = f" · your accounts: {bull} bullish / {bear} bearish posts (12h)" if rows else ""
    return f"Market: {' · '.join(bits) or 'prices pending'} · mood {word} ({mood:+.2f}){experts}"


def render_digest(rep: Report, signals: list[Signal], store, now: float | None = None,
                  previous: dict | None = None, max_watch: int = 6) -> str:
    now = now or time.time()
    lines = [_market_line(rep, store, now), ""]
    by = {b: [g for g in signals if g.bucket == b] for b in ICON}

    def price_bit(g):
        if g.change_24h is None:
            return ""
        return f" · price {_fmt_price(g.price)} ({g.change_24h:+.0f}% 24h)" if g.price else f" · {g.change_24h:+.0f}% 24h"

    lines.append(f"{ICON['green']} IN FOCUS: strongest signals (ideas to research, not buy signals)")
    if by["green"]:
        for g in by["green"][:5]:
            lines.append(f"{g.label}{price_bit(g)}")
            lines += [f"  • {w}" for w in g.why[:5]]
            if g.risks:
                lines.append(f"  ⚠ {'; '.join(g.risks)}")
            lines.append(f"  Confirms if: {g.confirm}")
            lines.append(f"  Invalidated if: {g.invalidate}")
    else:
        lines.append("  none right now: no coin has 3+ independent signals with the price still flat")
    lines.append("")
    lines.append(f"{ICON['yellow']} ON THE RADAR")
    for g in by["yellow"][:max_watch]:
        lines.append(f"{g.label}{price_bit(g)}: {'; '.join(g.why[:2])}"
                     + (f"  ⚠ {g.risks[0]}" if g.risks else ""))
    if not by["yellow"]:
        lines.append("  nothing building")
    lines.append("")
    if by["red"]:
        lines.append(f"{ICON['red']} LATE (already moved; don't chase): " + ", ".join(
            f"{g.label.split(' ·')[0]} {g.change_24h:+.0f}%" if g.change_24h is not None else g.label.split(' ·')[0]
            for g in by["red"][:8]))
    if by["black"]:
        lines.append(f"{ICON['black']} LIKELY PUSHED (avoid): " + ", ".join(
            g.label.split(' ·')[0] for g in by["black"][:10]))
    if previous is not None:
        now_b = {g.key: g.bucket for g in signals if g.bucket in ("green", "yellow")}
        labels = {g.key: g.label.split(" ·")[0] for g in signals}
        new = [labels[k] + ("🟢" if b == "green" else "") for k, b in now_b.items() if previous.get(k) != b]
        gone = [k for k in previous if k not in now_b]
        if new or gone:
            lines.append("")
            lines.append("Changed since last digest: "
                         + (f"new/upgraded {', '.join(new[:8])}" if new else "")
                         + ("; " if new and gone else "")
                         + (f"dropped {', '.join(gone[:8])}" if gone else ""))
    lines.append("")
    lines.append(render_scorecard(scorecard(store, now), trend_split(store, now)))
    lines.append("Not financial advice: this is chatter and data, not a buy signal.")
    return "\n".join(lines)


def render_green_alert(g: Signal) -> str:
    lines = [f"🟢 {g.label}: in focus (strongest signals; research it, not a buy signal)"]
    lines += [f"• {w}" for w in g.why[:5]]
    if g.price:
        lines.append(f"price {_fmt_price(g.price)}" + (f" ({g.change_24h:+.0f}% 24h)" if g.change_24h is not None else ""))
    if g.risks:
        lines.append(f"⚠ {'; '.join(g.risks)}")
    lines.append(f"Confirms if: {g.confirm}")
    lines.append(f"Invalidated if: {g.invalidate}")
    return "\n".join(lines)
