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

    def prune(self, keep_days: float) -> int:
        """Drop posts (and their mentions) older than keep_days. Coin first-seen
        dates only look back that far afterwards, so keep it well above the baseline."""
        cutoff = time.time() - keep_days * 86400
        self.db.execute("DELETE FROM mentions WHERE post_id IN "
                        "(SELECT id FROM posts WHERE created_utc < ?)", (cutoff,))
        deleted = self.db.execute("DELETE FROM posts WHERE created_utc < ?", (cutoff,)).rowcount
        self.db.execute("DELETE FROM alerts WHERE sent_utc < ?", (cutoff,))
        self.db.execute("DELETE FROM search_trends WHERE fetched_utc < ?", (cutoff,))
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

    def search_rows(self, since_utc: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM search_trends WHERE fetched_utc >= ? ORDER BY fetched_utc",
            (since_utc,),
        ).fetchall()

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
                      p.created_utc, p.sentiment
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
