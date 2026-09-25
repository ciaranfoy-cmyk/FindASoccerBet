# Crypto Radar

Shows which coins people are talking about across Reddit, Telegram, 4chan /biz/,
crypto news and (optionally) X, and which ones are **heating up**: buzz rising
fast compared with the coin's own usual level.

It covers any coin, including brand-new tokens that only exist as a `$TICKER`
or a contract address. It uses only the Python standard library, and every
source except X is free with no API key.

```
== HEATING UP (buzz rising fastest vs its own baseline) ==
coin                               voic posts  vs base  sentiment  src   top channels
HYPE · Hyperliquid                    9     9   53.7x  ++ +0.79  RTX   x:query, r/CryptoMoonShots, t.me/alpha
$GRUMPY (unlisted)                    4     4    new   ++ +0.76  T     t.me/degen_calls
```

## Quick start

Run from the repo root (Python 3.10+):

```bash
python -m crypto_radar collect      # fetch posts (takes ~2-3 min: polite rate limits)
python -m crypto_radar report       # rankings
python -m crypto_radar posts pepe   # read the actual posts behind a coin
```

Collect regularly so the baseline builds up. "vs base" and "new" become
meaningful after a day or two of history. For example, with cron:

```
*/15 * * * * cd /path/to/FindASoccerBet && python3 -m crypto_radar run >> crypto_radar/data/run.log 2>&1
```

or leave it running in a terminal: `python -m crypto_radar run --every 15`.

`run` = collect + send a Telegram alert when a coin crosses the "heating up"
threshold (at most once per coin per 12h).

## Sources

| Source | How | Needs |
|---|---|---|
| Reddit | newest posts + comments per subreddit | nothing (optional `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` from a free "script" app at reddit.com/prefs/apps: more reliable) |
| Telegram | public channel web previews (`t.me/s/<channel>`) | nothing (public **channels** only, not groups) |
| 4chan /biz/ | catalog + busiest threads | nothing |
| News | RSS (CoinDesk, Cointelegraph, Decrypt) | nothing |
| X / Twitter | v2 recent search | `X_BEARER_TOKEN` (paid API); skipped if unset |
| CoinGecko trending | top ~15 coins people are *searching* for, snapshotted every run | nothing |
| Google Trends | UK + US "trending now" searches; flags any coin that goes mainstream | nothing |
| CoinGecko | coin list (top 1000) | nothing |
| DEX Screener | turns posted contract addresses into token / chain / liquidity / 24h % | nothing |

Edit **`config.json`** to change subreddits, Telegram channels, feeds and X
queries. The Telegram list is only a starter set of big news channels. The
early chatter is in smaller "calls"/alpha channels, so add the ones you find
(any public channel username works).

## How it scores

- **Mentions**: `$TICKER` cashtags (any coin), contract addresses (EVM `0x…`
  and Solana, resolved via DEX Screener), bare tickers like `LINK` for the top
  250 coins (with a stoplist for words like ONE, SEC, ETF), and coin names
  like "Solana". Unknown cashtags show as `$FOO (unlisted)`.
- **Voices** = distinct (author, hour) pairs. One account spamming a ticker
  50 times counts once; 20 different people count 20.
- **vs base** = voices per hour in the window (default 6h) ÷ voices per hour in
  the baseline (default the previous 72h).
- **Heating up** = ranked by `log2(vs base) × √voices`, with a bonus for being
  on more than one platform.
- **New on the radar** = first time the coin has ever been mentioned in the DB.
- **Sentiment** = a crypto-slang lexicon (moon, rug, ngmi, 🚀, 💀 …), averaged
  over posts, from −1 to +1. It's noisy per post and more useful as an average.
- **Early search signals**: every run also records price, 1h and 24h change
  for everything on CoinGecko's trending list. A coin in the top 10 that is new
  to the list (last 3h) or has climbed 5+ places in ~2h, while its 24h price
  move is still under +15%, is flagged `EARLY?`, listed under "EARLY SEARCH
  SIGNALS" and sent as a 🔎 Telegram alert (once per coin per 12h). Coins that
  have already moved 15%+ are marked "searched after a pump". Search-only
  interest can mean a pump organised elsewhere, so treat these as "go and look".
- **Signal/pump channels** (`pump_channels` in `config.json`): coins mentioned
  *only* by these are kept out of "heating up", "new" and alerts, and listed on
  one line under "ONLY IN SIGNAL/PUMP CHANNELS". A coin that real people also
  talk about is ranked normally.
- **Dropping a channel for good** (e.g. a scam): remove it from
  `telegram_channels` and add it to `purge_channels`; its stored posts are
  deleted on the next run.
- Changing the extraction rules? Bump `EXTRACTOR_VERSION` in `extract.py` and
  every stored post is re-scanned on the next run (takes about a second).
- **Search interest**: `CG#3` = #3 on CoinGecko's trending searches, `↑new` = it
  entered the list within the window, `GOOGLE-TRENDING` = it hit Google's
  trending searches in the last 24h. Coins entering the search list, or hitting
  Google, get a boost in "heating up" because search confirming chatter is a
  stronger signal than either alone. The report's last section lists everything
  people are searching for, including coins nobody is talking about yet.

## Alerts to your phone

```bash
export TELEGRAM_BOT_TOKEN=...   # from @BotFather
export TELEGRAM_CHAT_ID=...     # see alerts.py for how to find it
python -m crypto_radar report --send   # test it
```

Thresholds (`alert_min_heat`, `alert_min_voices`, `alert_cooldown_hours`) are
in `config.json`.

## Tests

```bash
python -m unittest discover -s crypto_radar/tests -t .
```

## Caveats

- A lot of early crypto "hype" is coordinated shilling. Look at the posts (`posts <coin>`)
  and at the liquidity before trusting a spike. This is a radar, not a buy signal.
- Data lives in `crypto_radar/data/radar.sqlite3` (git-ignored).
