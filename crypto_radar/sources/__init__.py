"""Collectors. Each returns a list of Post; failures are logged and skipped, never fatal."""

from dataclasses import dataclass


@dataclass
class Post:
    id: str            # globally unique, prefixed by source, e.g. "reddit:t1_abc123"
    source: str        # reddit | telegram | 4chan | news | x
    channel: str       # where it was posted, e.g. "r/CryptoCurrency", "t.me/whale_alert_io"
    author: str        # who posted it (the channel itself for Telegram channels / news)
    created_utc: float
    text: str
    url: str = ""
