"""Crypto-slang-aware lexicon sentiment, scored in [-1, 1].

Generic sentiment tools read "this is going to zero lol" as neutral-to-positive
and "ape in" as nonsense. This small lexicon covers the slang that actually
shows up in crypto chatter. Crude, but fast and dependency-free; per-post
scores are noisy, averages over many posts are what matter.
"""

import math
import re

WORDS = {
    # bullish
    "bullish": 2.0, "bull": 1.0, "moon": 2.0, "mooning": 2.5, "moonshot": 2.0,
    "pump": 1.0, "pumping": 1.5, "rip": 0.5, "ripping": 2.0, "send": 1.0, "sending": 1.5,
    "gem": 2.0, "gems": 1.5, "undervalued": 1.5, "breakout": 1.5, "rally": 1.5,
    "ath": 1.5, "buy": 0.8, "buying": 1.0, "bought": 0.8, "accumulate": 1.2,
    "accumulating": 1.2, "long": 0.8, "hodl": 1.0, "hold": 0.5, "holding": 0.5,
    "wagmi": 2.0, "lfg": 2.0, "based": 1.0, "alpha": 1.0, "early": 1.0,
    "ape": 1.0, "aped": 1.0, "aping": 1.0, "10x": 2.0, "100x": 2.5, "1000x": 2.5,
    "green": 1.0, "up": 0.3, "huge": 1.0, "massive": 1.0, "strong": 1.2,
    "love": 1.5, "great": 1.5, "good": 1.0, "amazing": 2.0, "win": 1.2, "winning": 1.5,
    "profit": 1.2, "profits": 1.2, "gains": 1.5, "partnership": 1.0, "listing": 1.0,
    "listed": 1.0, "adoption": 1.0, "legit": 1.2, "solid": 1.0, "bullrun": 2.0,
    # bearish
    "bearish": -2.0, "bear": -1.0, "dump": -1.5, "dumping": -2.0, "dumped": -1.5,
    "rug": -3.0, "rugged": -3.0, "rugpull": -3.0, "scam": -3.0, "scammer": -3.0,
    "ponzi": -2.5, "honeypot": -3.0, "rekt": -2.0, "ngmi": -2.0, "dead": -2.0,
    "crash": -2.0, "crashing": -2.5, "crashed": -2.0, "sell": -0.8, "selling": -1.0,
    "sold": -0.5, "short": -0.8, "shorting": -1.0, "red": -1.0, "down": -0.3,
    "bleeding": -2.0, "liquidated": -2.0, "liquidation": -1.5, "fud": -0.5,
    "overvalued": -1.5, "bagholder": -1.5, "bagholders": -1.5, "bags": -0.5,
    "exit": -0.5, "hack": -2.5, "hacked": -3.0, "exploit": -2.5, "exploited": -3.0,
    "fraud": -3.0, "lawsuit": -1.5, "delist": -2.0, "delisted": -2.0, "delisting": -2.0,
    "worthless": -2.5, "trash": -2.0, "garbage": -2.0, "shitcoin": -1.5, "avoid": -1.5,
    "bad": -1.5, "terrible": -2.0, "hate": -1.5, "loss": -1.2, "losses": -1.2,
    "fake": -2.0, "careful": -0.8, "warning": -1.0, "capitulation": -2.0,
}

PHRASES = {
    "to the moon": 2.5, "send it": 2.0, "all time high": 1.5, "buy the dip": 1.5,
    "load up": 1.5, "loading up": 1.5, "going up": 1.2, "next leg up": 2.0,
    "going to zero": -3.0, "to zero": -2.5, "rug pull": -3.0, "pump and dump": -2.5,
    "exit liquidity": -2.5, "stay away": -2.5, "going down": -1.2, "sell off": -1.5,
    "dead cat": -1.5, "not financial advice": 0.0,
}

EMOJI = {
    "🚀": 1.5, "🌙": 1.5, "🔥": 1.0, "💎": 1.0, "🙌": 0.8, "📈": 1.2, "🟢": 0.8,
    "💰": 0.8, "🐂": 1.0, "💪": 0.8, "🤑": 1.0,
    "📉": -1.2, "💀": -1.2, "🤡": -1.2, "🔴": -0.8, "🐻": -1.0, "⚠️": -1.0, "🚨": -0.5,
    "😭": -0.5, "🩸": -1.2,
}

NEGATIONS = {"not", "no", "never", "dont", "don't", "isnt", "isn't", "wont", "won't",
             "aint", "ain't", "cant", "can't", "neither", "nor", "without"}

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_ALPHA = 15.0  # VADER-style normaliser: raw / sqrt(raw^2 + alpha)


def score(text: str) -> float:
    lower = text.lower()
    raw = 0.0
    for phrase, weight in PHRASES.items():
        if phrase in lower:
            raw += weight * lower.count(phrase)
            lower = lower.replace(phrase, " ")
    for emoji, weight in EMOJI.items():
        raw += weight * min(text.count(emoji), 3)  # cap emoji spam

    tokens = _TOKEN_RE.findall(lower)
    for i, tok in enumerate(tokens):
        weight = WORDS.get(tok)
        if weight is None:
            continue
        if any(t in NEGATIONS for t in tokens[max(0, i - 3):i]):
            weight *= -0.7
        raw += weight

    if raw == 0:
        return 0.0
    return raw / math.sqrt(raw * raw + _ALPHA)
