"""Coin registry: maps tickers and names to a canonical coin.

Pulls the top N coins by market cap from CoinGecko's free API (no key) and
caches them for a day. Falls back to a small built-in list if CoinGecko is
unreachable, so the rest of the pipeline still works offline.
"""

import json
import os
import time
from dataclasses import dataclass

from . import net

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
REGISTRY_CACHE = os.path.join(CACHE_DIR, "coingecko_markets.json")
REGISTRY_TTL = 24 * 3600
COINGECKO = "https://api.coingecko.com/api/v3"


@dataclass(frozen=True)
class Coin:
    id: str          # CoinGecko id, e.g. "bitcoin"
    symbol: str      # upper-case ticker, e.g. "BTC"
    name: str        # e.g. "Bitcoin"
    rank: int        # market-cap rank (1 = biggest); 9999 if unknown


# (id, symbol, name) in rough market-cap order. Only used when CoinGecko
# can't be reached; the live list covers far more.
_FALLBACK = [
    ("bitcoin", "BTC", "Bitcoin"), ("ethereum", "ETH", "Ethereum"),
    ("tether", "USDT", "Tether"), ("ripple", "XRP", "XRP"),
    ("binancecoin", "BNB", "BNB"), ("solana", "SOL", "Solana"),
    ("usd-coin", "USDC", "USDC"), ("dogecoin", "DOGE", "Dogecoin"),
    ("tron", "TRX", "TRON"), ("cardano", "ADA", "Cardano"),
    ("hyperliquid", "HYPE", "Hyperliquid"), ("chainlink", "LINK", "Chainlink"),
    ("sui", "SUI", "Sui"), ("stellar", "XLM", "Stellar"),
    ("avalanche-2", "AVAX", "Avalanche"), ("bitcoin-cash", "BCH", "Bitcoin Cash"),
    ("hedera-hashgraph", "HBAR", "Hedera"), ("litecoin", "LTC", "Litecoin"),
    ("shiba-inu", "SHIB", "Shiba Inu"), ("the-open-network", "TON", "Toncoin"),
    ("polkadot", "DOT", "Polkadot"), ("monero", "XMR", "Monero"),
    ("pepe", "PEPE", "Pepe"), ("uniswap", "UNI", "Uniswap"),
    ("aave", "AAVE", "Aave"), ("near", "NEAR", "NEAR Protocol"),
    ("aptos", "APT", "Aptos"), ("internet-computer", "ICP", "Internet Computer"),
    ("ethereum-classic", "ETC", "Ethereum Classic"), ("ondo-finance", "ONDO", "Ondo"),
    ("bittensor", "TAO", "Bittensor"), ("render-token", "RENDER", "Render"),
    ("arbitrum", "ARB", "Arbitrum"), ("cosmos", "ATOM", "Cosmos Hub"),
    ("filecoin", "FIL", "Filecoin"), ("injective-protocol", "INJ", "Injective"),
    ("optimism", "OP", "Optimism"), ("bonk", "BONK", "Bonk"),
    ("dogwifcoin", "WIF", "dogwifhat"), ("floki", "FLOKI", "FLOKI"),
    ("jupiter-exchange-solana", "JUP", "Jupiter"), ("pudgy-penguins", "PENGU", "Pudgy Penguins"),
    ("fartcoin", "FARTCOIN", "Fartcoin"), ("official-trump", "TRUMP", "Official Trump"),
    ("worldcoin-wld", "WLD", "Worldcoin"), ("sei-network", "SEI", "Sei"),
    ("kaspa", "KAS", "Kaspa"), ("algorand", "ALGO", "Algorand"),
    ("ethena", "ENA", "Ethena"), ("mantle", "MNT", "Mantle"),
]


def _fetch_markets(top_n: int) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while len(rows) < top_n:
        batch = net.get_json(
            f"{COINGECKO}/coins/markets?vs_currency=usd&order=market_cap_desc"
            f"&per_page=250&page={page}"
        )
        if not batch:
            break
        rows.extend(batch)
        page += 1
    return rows[:top_n]


def _read_cache() -> tuple[float, list[Coin]] | None:
    """(fetched_at, coins). The timestamp lives in the file, not its mtime, so the
    cache survives being copied around (e.g. between CI runs)."""
    try:
        with open(REGISTRY_CACHE) as f:
            data = json.load(f)
        return data["fetched_at"], [Coin(**c) for c in data["coins"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_registry(top_n: int = 1000, refresh: bool = False) -> list[Coin]:
    """Return the top_n coins, from cache if fresh, else CoinGecko, else fallback."""
    cached = _read_cache()
    if not refresh and cached and time.time() - cached[0] < REGISTRY_TTL:
        return cached[1]
    try:
        rows = _fetch_markets(top_n)
        coins = [
            Coin(id=r["id"], symbol=r["symbol"].upper(), name=r["name"],
                 rank=r.get("market_cap_rank") or 9999)
            for r in rows
        ]
    except net.HttpError as exc:
        print(f"[coins] CoinGecko unavailable ({exc}); using "
              f"{'stale cache' if cached else 'built-in list'}")
        return cached[1] if cached else fallback_registry()  # stale cache beats the tiny fallback

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(REGISTRY_CACHE, "w") as f:
        json.dump({"fetched_at": time.time(), "coins": [c.__dict__ for c in coins]}, f)
    return coins


def fallback_registry() -> list[Coin]:
    return [Coin(id=i, symbol=s, name=n, rank=r)
            for r, (i, s, n) in enumerate(_FALLBACK, start=1)]
