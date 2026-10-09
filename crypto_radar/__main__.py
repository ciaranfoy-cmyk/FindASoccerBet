"""Command-line entry point.

  python -m crypto_radar collect            # fetch new posts from all sources
  python -m crypto_radar report             # what's being talked about / heating up
  python -m crypto_radar posts pepe         # show the actual posts behind a coin
  python -m crypto_radar run --every 15     # collect + alert on a loop
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import alerts, coins, dexscreener, report, search, sentiment, signals
from .extract import EXTRACTOR_VERSION, Extractor
from .sources import fourchan, news, reddit, telegram, x, youtube
from .store import DEFAULT_DB, Store

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
ALL_SOURCES = ("reddit", "telegram", "4chan", "news", "x", "youtube", "search")


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def collect(store: Store, cfg: dict, sources: tuple[str, ...]) -> int:
    registry = coins.load_registry(cfg.get("coin_registry_size", 1000))
    extractor = Extractor(registry)
    if store.get_state("extractor_version") != EXTRACTOR_VERSION:
        n = store.reextract(extractor.extract)
        store.set_state("extractor_version", EXTRACTOR_VERSION)
        store.commit()
        print(f"[collect] extraction rules changed: re-scanned {n} stored posts")

    posts = []
    if "reddit" in sources:
        subs = cfg.get("reddit_subreddits", [])
        per_run = cfg.get("reddit_subreddits_per_run") or len(subs)
        if subs and per_run < len(subs):
            # Rotate through the list across runs to stay under Reddit's rate limit.
            start = store.get_state("reddit_offset", 0) % len(subs)
            subs = (subs + subs)[start:start + per_run]
            store.set_state("reddit_offset", start + per_run)
        posts += reddit.collect(subs)
    if "telegram" in sources:
        posts += telegram.collect(cfg.get("telegram_channels", []))
    if "4chan" in sources and cfg.get("fourchan_board"):
        posts += fourchan.collect(cfg["fourchan_board"], cfg.get("fourchan_full_threads", 15))
    if "news" in sources:
        posts += news.collect(cfg.get("news_feeds", []))
    if store.get_state("x_cleanup") != 1:
        # One-off: relabel old general-search posts and drop spam stored before the filter.
        n = store.cleanup_x(x.is_spam)
        store.set_state("x_cleanup", 1)
        store.commit()
        print(f"[x] cleanup: {n} spam posts removed")
    if "x" in sources and os.environ.get("X_BEARER_TOKEN"):
        posts += collect_x(store, cfg, registry)

    if "youtube" in sources and os.environ.get("YOUTUBE_API_KEY"):
        cache = store.get_state("youtube_channels", {})
        yt_posts, videos = youtube.collect(cfg.get("youtube_channels", []), cache, time.time())
        store.set_state("youtube_channels", cache)
        store.save_videos(videos, time.time())
        posts += yt_posts

    if "search" in sources:
        search.collect(store, registry, cfg.get("google_trends_geos", ["GB", "US"]))
        if os.environ.get("LUNARCRUSH_API_KEY"):
            search.collect_lunarcrush(store, registry, cfg.get("lunarcrush_top", 200))

    new = 0
    by_source: dict[str, int] = {}
    for post in posts:
        mentions = extractor.extract(post.text)
        if store.add_post(post, sentiment.score(post.text), mentions):
            new += 1
            by_source[post.source] = by_source.get(post.source, 0) + 1
    store.commit()

    # Look up any contract addresses people posted in the last day.
    pending = store.unresolved_contracts(since_utc=time.time() - 86400)
    if pending:
        for key, info in dexscreener.resolve(pending).items():
            store.save_token_info(key, info)
        store.commit()

    summary = ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())) or "nothing new"
    print(f"[collect] {len(posts)} fetched, {new} new ({summary}); "
          f"{len(pending)} contract addresses looked up")
    return new


def collect_x(store: Store, cfg: dict, registry) -> list:
    """X bills per post read, so: only every few hours, watched accounts first, a small
    general sample after, and a hard daily cap on posts read across everything."""
    every = cfg.get("x_every_hours", 3) * 3600
    forced = os.environ.get("X_FORCE") == "1"  # manual "check X now" run
    if not forced and time.time() - store.get_state("x_last_run", 0) < every - 300:
        return []
    store.set_state("x_last_run", time.time())

    search.collect_x_trends(store, registry, cfg.get("x_trend_locations", ["WORLD", "UK", "US"]))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    used = store.get_state("x_posts_read", {}).get(today, 0)
    budget = cfg.get("x_daily_post_budget", 250)
    since_ids = store.get_state("x_since_ids", {})
    watched = cfg.get("x_accounts", []) + cfg.get("x_signal_accounts", [])
    plan = [(q, cfg.get("x_max_posts_per_run", 100)) for q in x.account_queries(watched)]
    plan += [(x.search_query(q), cfg.get("x_sample_posts_per_run", 20)) for q in cfg.get("x_queries", [])]
    got = []
    for query, cap in plan:
        cap = min(cap, budget - used)
        if cap < 10:  # the API's minimum page is 10
            print(f"[x] daily budget reached ({used}/{budget} posts read today); skipping the rest")
            break
        kept, read = x.collect([query], since_ids, cap)
        got += kept
        used += read
    store.set_state("x_since_ids", since_ids)
    store.set_state("x_posts_read", {today: used})
    print(f"[x] {len(got)} posts kept; {used}/{budget} posts read today")
    return got


def news_channels(cfg: dict) -> frozenset:
    return frozenset(f"t.me/{c}" for c in cfg.get("news_channels", []))


def pump_channels(cfg: dict) -> frozenset:
    return frozenset([f"t.me/{c}" for c in cfg.get("pump_channels", [])]
                     + [f"x:@{a.strip().lstrip('@').lower()}" for a in cfg.get("x_signal_accounts", [])])


def build_report(store: Store, cfg: dict, window: float | None = None,
                 baseline: float | None = None) -> report.Report:
    rep = report.build(store, window or cfg["report_window_hours"],
                       baseline or cfg["report_baseline_hours"],
                       pump_channels=pump_channels(cfg), news_channels=news_channels(cfg),
                       brand_channels=frozenset(f"x:@{a.lower()}" for a in cfg.get("brand_accounts", [])))
    rep.hidden = frozenset(cfg.get("market_coins", []))
    return rep


def digest_due(store: Store, cfg: dict, now: float) -> bool:
    """True once per scheduled hour (UK time) for the Telegram digest."""
    local = datetime.fromtimestamp(now, ZoneInfo(cfg.get("digest_timezone", "Europe/London")))
    if local.hour not in cfg.get("digest_hours", [8, 14]):
        return False
    slot = local.strftime("%Y-%m-%d %H")
    if store.get_state("digest_sent") == slot:
        return False
    store.set_state("digest_sent", slot)
    return True


def check_alerts(store: Store, cfg: dict) -> None:
    now = time.time()
    rep = build_report(store, cfg)
    cooldown = cfg.get("alert_cooldown_hours", 12) * 3600

    # Prices for anything with a signal (and open calls), then score and log the calls.
    registry_ids = {c.id for c in coins.load_registry(cfg.get("coin_registry_size", 1000))}
    ids = signals.price_candidates(rep, registry_ids, signals.open_call_ids(store, registry_ids, now))
    search.fetch_prices(store, ids)
    rep = build_report(store, cfg)
    sigs = signals.build_signals(rep, store, now)
    for g in signals.record(store, sigs, now):
        key = f"green:{g.key}"
        last = store.last_alert(key)
        if last and now - last < cooldown:
            continue
        alerts.send(signals.render_green_alert(g))
        store.record_alert(key)

    if digest_due(store, cfg, now):
        previous = store.get_state("digest_buckets", {})
        alerts.send(signals.render_digest(rep, sigs, store, now, previous))
        store.set_state("digest_buckets", {g.key: g.bucket for g in sigs if g.bucket in ("green", "yellow")})

    # Raw heating-up / search alerts only if asked for (the green alerts replace them).
    if cfg.get("raw_alerts", False):
        for s in rep.heating_up(min_voices=cfg.get("alert_min_voices", 5)):
            if s.heat < cfg.get("alert_min_heat", 4.0):
                continue
            last = store.last_alert(s.key)
            if last and now - last < cooldown:
                continue
            alerts.send(report.render_alert(s))
            store.record_alert(s.key)
    store.commit()


def main() -> None:
    parser = argparse.ArgumentParser(prog="crypto_radar", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--db", default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="fetch new posts from all sources")
    p.add_argument("--sources", default=",".join(ALL_SOURCES),
                   help=f"comma-separated subset of {','.join(ALL_SOURCES)}")

    p = sub.add_parser("report", help="rank coins by buzz")
    p.add_argument("--window", type=float, help="hours (default from config)")
    p.add_argument("--baseline", type=float, help="hours (default from config)")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--send", action="store_true", help="also send the report to Telegram")

    p = sub.add_parser("digest", help="one-screen digest: early/watch/late/pushed calls")
    p.add_argument("--send", action="store_true", help="also send it to Telegram")

    p = sub.add_parser("signals", help="every coin's score, bucket and evidence")

    p = sub.add_parser("posts", help="show recent posts mentioning a coin")
    p.add_argument("coin", help="ticker, name, $CASHTAG, CoinGecko id or contract address")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("run", help="collect + alert, once or on a loop")
    p.add_argument("--every", type=float, default=0, help="minutes between runs (0 = run once)")
    p.add_argument("--sources", default=",".join(ALL_SOURCES))

    sub.add_parser("refresh-coins", help="re-download the CoinGecko coin list")

    args = parser.parse_args()
    cfg = load_config(args.config)
    store = Store(args.db)

    if args.cmd == "collect":
        collect(store, cfg, tuple(args.sources.split(",")))

    elif args.cmd == "report":
        rep = build_report(store, cfg, args.window, args.baseline)
        text = report.render_text(rep, args.top)
        print(text)
        if args.send:
            alerts.send("\n".join(
                f"{i}. {s.label} — {s.voices} voices, {s.velocity:.1f}x, sent {s.sentiment:+.2f}"
                for i, s in enumerate(rep.most_talked(15), 1)))

    elif args.cmd == "digest":
        rep = build_report(store, cfg)
        sigs = signals.build_signals(rep, store)
        text = signals.render_digest(rep, sigs, store, previous=store.get_state("digest_buckets", {}))
        print(text)
        if args.send:
            alerts.send(text)

    elif args.cmd == "signals":
        rep = build_report(store, cfg)
        for g in signals.build_signals(rep, store):
            print(f"{signals.ICON[g.bucket]} {g.score:4.1f} {g.label[:30]:<30} kinds={','.join(sorted(g.kinds))} "
                  f"24h={g.change_24h if g.change_24h is None else round(g.change_24h, 1)}")
            for w in g.why:
                print(f"      + {w}")
            for r in g.risks:
                print(f"      - {r}")

    elif args.cmd == "posts":
        keys = store.find_keys(args.coin)
        if not keys:
            print(f"No stored mentions match {args.coin!r}")
            return
        since = time.time() - args.hours * 3600
        for key in keys:
            print(f"== {key} ==")
            for row in store.posts_for(key, since, args.limit):
                when = datetime.fromtimestamp(row["created_utc"], timezone.utc).strftime("%m-%d %H:%M")
                text = " ".join(row["text"].split())[:220]
                print(f"[{when} {row['channel']} {row['sentiment']:+.2f}] {text}\n    {row['url']}")

    elif args.cmd == "run":
        sources = tuple(args.sources.split(","))
        while True:
            collect(store, cfg, sources)
            check_alerts(store, cfg)
            store.prune(cfg.get("keep_days", 21))
            store.purge_channels([f"t.me/{c}" for c in cfg.get("purge_channels", [])])
            if not args.every:
                break
            time.sleep(args.every * 60)

    elif args.cmd == "refresh-coins":
        registry = coins.load_registry(cfg.get("coin_registry_size", 1000), refresh=True)
        print(f"{len(registry)} coins loaded")


if __name__ == "__main__":
    main()
