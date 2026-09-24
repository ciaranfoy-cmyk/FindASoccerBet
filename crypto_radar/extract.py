"""Pull coin mentions out of free text.

Four kinds of mention, from most to least intentional:

  cashtag   "$PEPE", "$wif"      -> any coin, even ones not in the registry
  contract  "0xabc...", "7GCi...pump" (EVM / Solana addresses) -> brand-new tokens
  symbol    bare "SOL", "LINK"   -> only well-known coins, with a stoplist
  name      "Solana", "dogwifhat" -> only registry coins, with a stoplist

Coins in the registry are keyed by CoinGecko id; unknown cashtags by "$SYM";
contracts by "ca:<address>" (resolved to a name later via DEX Screener).
"""

import re
from dataclasses import dataclass

from .coins import Coin

# Upper-case words that are also tickers but are far more often just words,
# acronyms or crypto slang. Bare-symbol matches on these are ignored (a
# cashtag like "$ONE" still counts).
AMBIGUOUS_SYMBOLS = {
    "A", "AI", "ALL", "AND", "ANY", "API", "ARE", "ATH", "ATL", "BAD", "BEST", "BIG",
    "BTFD", "BUY", "CAT", "CEO", "CEX", "CPI", "DAO", "DCA", "DEX", "DOG", "EDGE",
    "ETF", "EUR", "FED", "FOMO", "FOR", "FUD", "FUN", "GAS", "GBP", "GDP", "GET", "GM",
    "GN", "GOOD", "HIGH", "HODL", "HOT", "IMO", "IPO", "IT", "JUST", "KEY", "KYC",
    "LOL", "LOW", "MAX", "ME", "MOON", "NEW", "NFT", "NGMI", "NOT", "NOW", "OK", "OMG",
    "ONE", "OPEN", "OUT", "PUMP", "REAL", "SAFE", "SEC", "SELL", "SUN", "THE", "TIME",
    "TVL", "USA", "USD", "WAGMI", "WAR", "WIN", "WTF", "YOU", "APY", "APR", "ROI",
    "PNL", "OTC", "RWA", "L1", "L2", "TLDR",
}

# Stablecoins: constantly mentioned (whale transfers, "sold to USDT") but never
# the story. Dropped however they're mentioned.
STABLECOINS = {
    "USDT", "USDC", "DAI", "FDUSD", "USDE", "USDS", "RLUSD", "PYUSD", "TUSD", "BUSD",
    "USD1", "USDD", "GHO", "FRAX", "USDY", "USDX", "EURC", "USD0", "USDG", "USDF",
}

# Cashtags that aren't coins at all.
NON_COIN_CASHTAGS = {
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY",
    "SPY", "QQQ", "SPX", "DXY", "VIX",
}

# Lower-case tickers people commonly type without a $ ("long eth", "sol is ripping").
LOWERCASE_SYMBOLS = {
    "btc", "eth", "sol", "xrp", "doge", "bnb", "ada", "shib", "pepe", "avax",
    "ltc", "trx", "sui", "hbar", "xlm", "bonk", "wif", "kas", "tao",
}

# Coin names that are also everyday English words. Only matched via cashtag or symbol.
COMMON_WORDS = {
    "just", "status", "harmony", "origin", "synapse", "magic", "band", "civic", "oasis",
    "radiant", "spell", "prime", "render", "optimism", "stellar", "flow", "story",
    "movement", "sonic", "cosmos", "core", "sky", "ocean", "gala", "threshold",
    "maker", "theta", "mantle", "celestia", "polygon", "jupiter", "dash", "zcash",
    "compound", "curve", "balancer", "convex", "frax", "lido", "blur", "venom",
    "echelon", "gold", "silver", "pi", "mask",
    "amp", "dusk", "saga", "ark", "wax", "velo", "wink", "pax", "usual", "plume",
}

_CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,11})\b")
# Trading pairs as signal channels write them: "#ACU/USDT", "BTC/USDT", "$SOL/USDC".
_PAIR_RE = re.compile(r"(?<![\w/])[#$]?([A-Za-z][A-Za-z0-9]{1,11})/(?:USDT|USDC|USD|BUSD|FDUSD|BTC|ETH)\b")
_UPPER_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})\b")
_LOWER_RE = re.compile(r"\b([a-z]{3,5})\b")
_EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


@dataclass(frozen=True)
class Mention:
    key: str      # canonical coin key
    symbol: str   # display ticker ("" for unresolved contracts)
    name: str     # display name
    method: str   # cashtag | contract | symbol | name


def _looks_like_solana_address(s: str) -> bool:
    # Real base58 addresses mix digits, upper and lower case; long words don't.
    return (any(c.isdigit() for c in s) and any(c.isupper() for c in s)
            and any(c.islower() for c in s))


class Extractor:
    def __init__(self, registry: list[Coin], bare_symbol_max_rank: int = 250,
                 name_max_rank: int = 500, lowercase_name_max_rank: int = 100):
        self.by_symbol: dict[str, Coin] = {}
        for coin in sorted(registry, key=lambda c: c.rank, reverse=True):
            self.by_symbol[coin.symbol] = coin  # lowest rank (biggest cap) wins collisions
        self.bare_symbol_max_rank = bare_symbol_max_rank

        # Names: exact capitalisation for most coins; also lower-case for the
        # top coins, which people routinely write as "bitcoin", "solana".
        self.by_name: dict[str, Coin] = {}
        for coin in sorted(registry, key=lambda c: c.rank, reverse=True):
            name = coin.name.strip()
            if coin.rank > name_max_rank or len(name) < 4 or name.lower() in COMMON_WORDS:
                continue
            if name.upper() == coin.symbol:
                continue  # e.g. "XRP", "BNB": the symbol path handles it
            self.by_name[name] = coin
            if coin.rank <= lowercase_name_max_rank:
                self.by_name[name.lower()] = coin
        names = sorted(self.by_name, key=len, reverse=True)  # longest first: "Bitcoin Cash" before "Bitcoin"
        self._name_re = (re.compile(r"(?<![\w$])(" + "|".join(re.escape(n) for n in names) + r")(?!\w)")
                         if names else None)

    def _coin_mention(self, coin: Coin, method: str) -> Mention:
        return Mention(key=coin.id, symbol=coin.symbol, name=coin.name, method=method)

    def extract(self, text: str) -> list[Mention]:
        found: dict[str, Mention] = {}

        def add(m: Mention) -> None:
            if m.symbol in STABLECOINS:
                return
            found.setdefault(m.key, m)  # first (most intentional) method wins

        for sym in _CASHTAG_RE.findall(text):
            sym = sym.upper()
            if sym in NON_COIN_CASHTAGS:
                continue
            coin = self.by_symbol.get(sym)
            if coin:
                add(self._coin_mention(coin, "cashtag"))
            else:
                add(Mention(key=f"${sym}", symbol=sym, name="", method="cashtag"))

        for sym in _PAIR_RE.findall(text):  # explicit trading pair: as intentional as a cashtag
            sym = sym.upper()
            coin = self.by_symbol.get(sym)
            if coin:
                add(self._coin_mention(coin, "cashtag"))
            elif sym not in NON_COIN_CASHTAGS:
                add(Mention(key=f"${sym}", symbol=sym, name="", method="cashtag"))

        for addr in _EVM_RE.findall(text):
            add(Mention(key=f"ca:{addr.lower()}", symbol="", name="", method="contract"))
        for addr in _SOL_RE.findall(text):
            if _looks_like_solana_address(addr):
                add(Mention(key=f"ca:{addr}", symbol="", name="", method="contract"))

        # Bare tickers/names: skip trading pairs so the quote side ("/BTC") isn't a mention.
        text = _PAIR_RE.sub(" ", text)
        for sym in _UPPER_RE.findall(text):
            coin = self.by_symbol.get(sym)
            if (coin and sym not in AMBIGUOUS_SYMBOLS and len(sym) >= 3
                    and coin.rank <= self.bare_symbol_max_rank):
                add(self._coin_mention(coin, "symbol"))
        for sym in _LOWER_RE.findall(text):
            if sym in LOWERCASE_SYMBOLS:
                coin = self.by_symbol.get(sym.upper())
                if coin:
                    add(self._coin_mention(coin, "symbol"))

        if self._name_re:
            for name in self._name_re.findall(text):
                add(self._coin_mention(self.by_name[name], "name"))

        return list(found.values())
