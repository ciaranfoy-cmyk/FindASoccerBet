"""Resolve contract addresses to token name/symbol/liquidity via DEX Screener (free, no key).

This is what turns "someone posted 7GCi...pump" into "$FOO on Solana, $80k
liquidity, +340% today" — and lets us drop addresses that aren't tokens at
all (wallets, random base58 strings).
"""

from . import net

API = "https://api.dexscreener.com/latest/dex/tokens"
BATCH = 30  # max addresses per request


def best_pairs(pairs: list[dict]) -> dict[str, dict]:
    """Map lower-cased token address -> summary of its most liquid trading pair."""
    best: dict[str, dict] = {}
    for pair in pairs or []:
        base = pair.get("baseToken") or {}
        addr = (base.get("address") or "").lower()
        liquidity = (pair.get("liquidity") or {}).get("usd") or 0
        if not addr or (addr in best and best[addr]["liquidity_usd"] >= liquidity):
            continue
        best[addr] = {
            "symbol": base.get("symbol", "").upper(),
            "name": base.get("name", ""),
            "chain": pair.get("chainId", ""),
            "liquidity_usd": liquidity,
            "volume_24h": (pair.get("volume") or {}).get("h24"),
            "price_change_24h": (pair.get("priceChange") or {}).get("h24"),
            "url": pair.get("url", ""),
        }
    return best


def resolve(keys: list[str]) -> dict[str, dict | None]:
    """keys are "ca:<address>". Returns key -> info, or None if DEX Screener doesn't know it."""
    out: dict[str, dict | None] = {}
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        addresses = [k.removeprefix("ca:") for k in chunk]
        try:
            data = net.get_json(f"{API}/{','.join(addresses)}")
        except net.HttpError as exc:
            print(f"[dexscreener] {exc}")
            continue  # leave unresolved; retried next run
        found = best_pairs(data.get("pairs"))
        for key, addr in zip(chunk, addresses):
            out[key] = found.get(addr.lower())
    return out
