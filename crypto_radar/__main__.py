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

from . import alerts, coins, dexscreener, report, sentiment
from .extract import Extractor
from .sources import fourchan, news, reddit, telegram, x
from .store import DEFAULT_DB, Store

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
ALL_SOURCES = ("reddit", "telegram", "4chan", "news", "x")


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def collect(store: Store, cfg: dict, sources: tuple[str, ...]) -> int:
    extractor = Extractor(coins.load_registry(cfg.get("coin_registry_size", 1000)))

    posts = []
    if "reddit" in sources:
        posts += reddit.collect(cfg.get("reddit_subreddits", []))
    if "telegram" in sources:
        posts += telegram.collect(cfg.get("telegram_channels", []))
    if "4chan" in sources and cfg.get("fourchan_board"):
        posts += fourchan.collect(cfg["fourchan_board"], cfg.get("fourchan_full_threads", 15))
    if "news" in sources:
        posts += news.collect(cfg.get("news_feeds", []))
    if "x" in sources:
        since_ids = store.get_state("x_since_ids", {})
        posts += x.collect(cfg.get("x_queries", []), since_ids)
        store.set_state("x_since_ids", since_ids)

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


def check_alerts(store: Store, cfg: dict) -> None:
    rep = report.build(store, cfg["report_window_hours"], cfg["report_baseline_hours"])
    cooldown = cfg.get("alert_cooldown_hours", 12) * 3600
    for s in rep.heating_up(min_voices=cfg.get("alert_min_voices", 5)):
        if s.heat < cfg.get("alert_min_heat", 4.0):
            continue
        last = store.last_alert(s.key)
        if last and time.time() - last < cooldown:
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
        rep = report.build(store, args.window or cfg["report_window_hours"],
                           args.baseline or cfg["report_baseline_hours"])
        rep.trending_ids = coins.coingecko_trending()
        text = report.render_text(rep, args.top)
        print(text)
        if args.send:
            alerts.send("\n".join(
                f"{i}. {s.label} — {s.voices} voices, {s.velocity:.1f}x, sent {s.sentiment:+.2f}"
                for i, s in enumerate(rep.most_talked(15), 1)))

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
            if not args.every:
                break
            time.sleep(args.every * 60)

    elif args.cmd == "refresh-coins":
        registry = coins.load_registry(cfg.get("coin_registry_size", 1000), refresh=True)
        print(f"{len(registry)} coins loaded")


if __name__ == "__main__":
    main()
