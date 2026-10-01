"""SQLite storage: posts, the coin mentions found in them, display labels, and small bits of state."""

import json
import os
import sqlite3
import time

from .extract import Mention
from .sources import Post

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "radar.sqlite3")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    author TEXT NOT NULL,
    created_utc REAL NOT NULL,
    text TEXT NOT NULL,
    url TEXT,
    sentiment REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_created ON posts(created_utc);

CREATE TABLE IF NOT EXISTS mentions (
    post_id TEXT NOT NULL REFERENCES posts(id),
    coin_key TEXT NOT NULL,
    method TEXT NOT NULL,
    PRIMARY KEY (post_id, coin_key)
);
CREATE INDEX IF NOT EXISTS mentions_coin ON mentions(coin_key);

-- Display info per coin key. For contract addresses, filled in from DEX Screener.
CREATE TABLE IF NOT EXISTS coins (
    key TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    chain TEXT,
    liquidity_usd REAL,
    volume_24h REAL,
    price_change_24h REAL,
    url TEXT,
    resolved_utc REAL
);

-- One row per coin per search-trend snapshot (CoinGecko trending, Google Trends).
CREATE TABLE IF NOT EXISTS search_trends (
    fetched_utc REAL NOT NULL,
    source TEXT NOT NULL,          -- coingecko | google-GB | google-US
    coin_key TEXT NOT NULL,
    rank INTEGER NOT NULL,         -- position on the list, 1 = top
    symbol TEXT,
    name TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS search_trends_time ON search_trends(fetched_utc);

CREATE TABLE IF NOT EXISTS prices (
    fetched_utc REAL NOT NULL,
    coin_key TEXT NOT NULL,
    price REAL,
    change_1h REAL,               -- percent
    change_24h REAL,              -- percent
    market_cap REAL
);
CREATE INDEX IF NOT EXISTS prices_coin_time ON prices(coin_key, fetched_utc);

-- Latest stats per followed YouTube video (refreshed every check).
CREATE TABLE IF NOT EXISTS youtube_videos (
    video_id TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    title TEXT,
    published_utc REAL,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    views_per_hour REAL,
    vs_norm REAL,               -- views/hour vs the channel's other recent uploads
    fetched_utc REAL
);

-- Every time a coin is called early (green) or watch (yellow), for the track record.
CREATE TABLE IF NOT EXISTS signal_log (
    coin_key TEXT NOT NULL,
    label TEXT,
    bucket TEXT NOT NULL,       -- green | yellow
    score REAL,
    logged_utc REAL NOT NULL,
    price REAL,                 -- price when called (None if unknown)
    why TEXT
);
CREATE INDEX IF NOT EXISTS signal_log_time ON signal_log(logged_utc);

CREATE TABLE IF NOT EXISTS alerts (
    coin_key TEXT NOT NULL,
    sent_utc REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str = DEFAULT_DB):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def add_post(self, post: Post, sentiment: float, mentions: list[Mention]) -> bool:
        """Insert a post and its mentions. Returns False if we'd already seen it."""
        created = post.created_utc or time.time()  # undated news items: use fetch time
        cur = self.db.execute(
            "INSERT OR IGNORE INTO posts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (post.id, post.source, post.channel, post.author, created, post.text, post.url, sentiment),
        )
        if cur.rowcount == 0:
            return False
        for m in mentions:
            self.db.execute("INSERT OR IGNORE INTO mentions VALUES (?, ?, ?)",
                            (post.id, m.key, m.method))
            if m.symbol or m.name:
                self.db.execute("INSERT OR IGNORE INTO coins (key, symbol, name) VALUES (?, ?, ?)",
                                (m.key, m.symbol, m.name))
        return True

    def commit(self) -> None:
        self.db.commit()

    def reextract(self, extract) -> int:
        """Re-run coin extraction over every stored post (extract: text -> [Mention])."""
        rows = self.db.execute("SELECT id, text FROM posts").fetchall()
        self.db.execute("DELETE FROM mentions")
        for post_id, text in rows:
            for m in extract(text):
                self.db.execute("INSERT OR IGNORE INTO mentions VALUES (?, ?, ?)",
                                (post_id, m.key, m.method))
                if m.symbol or m.name:
                    self.db.execute("INSERT OR IGNORE INTO coins (key, symbol, name) VALUES (?, ?, ?)",
                                    (m.key, m.symbol, m.name))
        self.db.commit()
        return len(rows)

    def purge_channels(self, channels: list[str]) -> int:
        """Delete everything collected from channels we've dropped (e.g. found to be scams)."""
        deleted = 0
        for ch in channels:
            self.db.execute("DELETE FROM mentions WHERE post_id IN "
                            "(SELECT id FROM posts WHERE channel = ?)", (ch,))
            deleted += self.db.execute("DELETE FROM posts WHERE channel = ?", (ch,)).rowcount
        self.db.commit()
        return deleted

    def cleanup_x(self, is_spam) -> int:
        """Relabel general X search posts as "x:search" and delete stored X spam."""
        self.db.execute("UPDATE posts SET channel = 'x:search' "
                        "WHERE source = 'x' AND channel NOT LIKE 'x:@%'")
        spam = [r[0] for r in self.db.execute("SELECT id, text FROM posts WHERE source = 'x'")
                if is_spam(r[1])]
        for post_id in spam:
            self.db.execute("DELETE FROM mentions WHERE post_id = ?", (post_id,))
            self.db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        return len(spam)

    def prune(self, keep_days: float) -> int:
        """Drop posts (and their mentions) older than keep_days. Coin first-seen
        dates only look back that far afterwards, so keep it well above the baseline."""
        cutoff = time.time() - keep_days * 86400
        self.db.execute("DELETE FROM mentions WHERE post_id IN "
                        "(SELECT id FROM posts WHERE created_utc < ?)", (cutoff,))
        deleted = self.db.execute("DELETE FROM posts WHERE created_utc < ?", (cutoff,)).rowcount
        self.db.execute("DELETE FROM alerts WHERE sent_utc < ?", (cutoff,))
        self.db.execute("DELETE FROM search_trends WHERE fetched_utc < ?", (cutoff,))
        self.db.execute("DELETE FROM prices WHERE fetched_utc < ?", (cutoff,))
        self.db.execute("DELETE FROM youtube_videos WHERE published_utc < ?", (cutoff,))
        self.db.execute("DELETE FROM signal_log WHERE logged_utc < ?", (cutoff,))
        self.db.commit()
        if deleted:
            self.db.execute("VACUUM")
        return deleted

    # --- search interest ------------------------------------------------------------

    def add_search_trend(self, fetched_utc: float, source: str, coin_key: str, rank: int,
                         symbol: str, name: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO search_trends VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (fetched_utc, source, coin_key, rank, symbol, name, detail))
        self.db.execute("INSERT OR IGNORE INTO coins (key, symbol, name) VALUES (?, ?, ?)",
                        (coin_key, symbol, name))

    def add_price(self, fetched_utc: float, coin_key: str, price, change_1h, change_24h,
                  market_cap) -> None:
        self.db.execute("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?)",
                        (fetched_utc, coin_key, price, change_1h, change_24h, market_cap))

    def latest_prices(self, keys: list[str], since_utc: float) -> dict[str, sqlite3.Row]:
        """Most recent price row per coin since `since_utc`."""
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        rows = self.db.execute(
            f"""SELECT * FROM prices WHERE coin_key IN ({q}) AND fetched_utc >= ?
                ORDER BY fetched_utc""", (*keys, since_utc)).fetchall()
        return {r["coin_key"]: r for r in rows}

    def price_near(self, key: str, t: float, tolerance_s: float = 6 * 3600) -> float | None:
        """Price of `key` recorded closest to time t (within tolerance)."""
        r = self.db.execute(
            """SELECT price FROM prices WHERE coin_key = ? AND price IS NOT NULL
               AND fetched_utc BETWEEN ? AND ? ORDER BY ABS(fetched_utc - ?) LIMIT 1""",
            (key, t - tolerance_s, t + tolerance_s, t)).fetchone()
        return r[0] if r else None

    def log_signal(self, key: str, label: str, bucket: str, score: float, t: float,
                   price: float | None, why: str) -> None:
        self.db.execute("INSERT INTO signal_log VALUES (?,?,?,?,?,?,?)",
                        (key, label, bucket, score, t, price, why))

    def last_signal(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM signal_log WHERE coin_key = ? "
                               "ORDER BY logged_utc DESC LIMIT 1", (key,)).fetchone()

    def signals_since(self, since_utc: float) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM signal_log WHERE logged_utc >= ? ORDER BY logged_utc",
                               (since_utc,)).fetchall()

    def price_rows(self, since_utc: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM prices WHERE fetched_utc >= ? ORDER BY fetched_utc", (since_utc,)
        ).fetchall()

    def search_rows(self, since_utc: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM search_trends WHERE fetched_utc >= ? ORDER BY fetched_utc",
            (since_utc,),
        ).fetchall()

    def save_videos(self, videos: list[dict], fetched_utc: float) -> None:
        for v in videos:
            self.db.execute("INSERT OR REPLACE INTO youtube_videos VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (v["id"], v["handle"], v["title"], v["published"], v["views"], v["likes"],
                             v["comments"], v["views_per_hour"], v.get("vs_norm"), fetched_utc))

    def recent_videos(self, since_utc: float) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM youtube_videos WHERE published_utc >= ? "
                               "ORDER BY published_utc DESC", (since_utc,)).fetchall()

    # --- contract-address resolution -------------------------------------------------

    def unresolved_contracts(self, since_utc: float, max_age_s: float = 3600) -> list[str]:
        """Contract keys mentioned since `since_utc` whose market info is missing or stale."""
        rows = self.db.execute(
            """SELECT DISTINCT m.coin_key FROM mentions m
               JOIN posts p ON p.id = m.post_id
               LEFT JOIN coins c ON c.key = m.coin_key
               WHERE m.coin_key LIKE 'ca:%' AND p.created_utc >= ?
                 AND (c.resolved_utc IS NULL OR c.resolved_utc < ?)""",
            (since_utc, time.time() - max_age_s),
        )
        return [r[0] for r in rows]

    def save_token_info(self, key: str, info: dict | None) -> None:
        info = info or {}
        self.db.execute(
            """INSERT INTO coins (key, symbol, name, chain, liquidity_usd, volume_24h,
                                  price_change_24h, url, resolved_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 symbol=excluded.symbol, name=excluded.name, chain=excluded.chain,
                 liquidity_usd=excluded.liquidity_usd, volume_24h=excluded.volume_24h,
                 price_change_24h=excluded.price_change_24h, url=excluded.url,
                 resolved_utc=excluded.resolved_utc""",
            (key, info.get("symbol"), info.get("name"), info.get("chain"), info.get("liquidity_usd"),
             info.get("volume_24h"), info.get("price_change_24h"), info.get("url"), time.time()),
        )

    # --- reads ----------------------------------------------------------------------

    def mention_rows(self, since_utc: float) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT m.coin_key, m.method, p.id, p.source, p.channel, p.author,
                      p.created_utc, p.sentiment, p.text, p.url
               FROM mentions m JOIN posts p ON p.id = m.post_id
               WHERE p.created_utc >= ?""",
            (since_utc,),
        ).fetchall()

    def first_seen(self, keys: list[str]) -> dict[str, float]:
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = self.db.execute(
            f"""SELECT m.coin_key, MIN(p.created_utc) FROM mentions m
                JOIN posts p ON p.id = m.post_id
                WHERE m.coin_key IN ({placeholders}) GROUP BY m.coin_key""",
            keys,
        )
        return {r[0]: r[1] for r in rows}

    def coin_info(self, keys: list[str]) -> dict[str, sqlite3.Row]:
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = self.db.execute(f"SELECT * FROM coins WHERE key IN ({placeholders})", keys)
        return {r["key"]: r for r in rows}

    def oldest_post_utc(self) -> float | None:
        return self.db.execute("SELECT MIN(created_utc) FROM posts").fetchone()[0]

    def posts_for(self, coin_key: str, since_utc: float, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT p.* FROM posts p JOIN mentions m ON m.post_id = p.id
               WHERE m.coin_key = ? AND p.created_utc >= ?
               ORDER BY p.created_utc DESC LIMIT ?""",
            (coin_key, since_utc, limit),
        ).fetchall()

    def find_keys(self, query: str) -> list[str]:
        """Resolve user input ("pepe", "$PEPE", "PEPE", a contract) to stored coin keys."""
        q = query.strip()
        if q.startswith("$"):
            q = q[1:]
        rows = self.db.execute(
            """SELECT key FROM coins WHERE key = ? OR key = ? OR key = ?
                   OR UPPER(symbol) = UPPER(?) OR LOWER(name) = LOWER(?)""",
            (q, f"${q.upper()}", f"ca:{q}", q, q),
        )
        return [r[0] for r in rows]

    def last_alert(self, coin_key: str) -> float | None:
        return self.db.execute("SELECT MAX(sent_utc) FROM alerts WHERE coin_key = ?",
                               (coin_key,)).fetchone()[0]

    def record_alert(self, coin_key: str) -> None:
        self.db.execute("INSERT INTO alerts VALUES (?, ?)", (coin_key, time.time()))

    def get_state(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))
