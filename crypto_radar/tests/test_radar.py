"""Offline tests: parsing fixtures, extraction, sentiment and scoring. Run from repo root:

    python -m unittest discover crypto_radar/tests
"""

import time
import unittest

from crypto_radar import coins, dexscreener, report, search, sentiment
from crypto_radar.extract import Extractor, Mention
from crypto_radar.sources import Post, fourchan, news, reddit, telegram, x
from crypto_radar.store import Store

EXTRACTOR = Extractor(coins.fallback_registry())


def keys(text):
    return {m.key for m in EXTRACTOR.extract(text)}


class ExtractTest(unittest.TestCase):
    def test_cashtags_known_and_unknown(self):
        self.assertEqual(keys("loading $SOL and $pepe, also $ZZZTOP"), {"solana", "pepe", "$ZZZTOP"})

    def test_fiat_and_prices_are_not_coins(self):
        self.assertEqual(keys("$USD strength, sold at $100 and $SPY"), set())

    def test_trading_pairs(self):
        self.assertEqual(keys("#ACU/USDT Take-Profit target 1 ✅, also LINK/BTC"), {"$ACU", "chainlink"})
        self.assertEqual(keys("USDT/USD peg holds"), set())

    def test_joined_trading_pairs(self):
        self.assertEqual(keys("Buy Limit SOLETH now, and #LINKUSDT long"), {"solana", "chainlink"})
        self.assertEqual(keys("new listing FOOUSDT"), {"$FOO"})
        # an unknown word that merely ends in ETH/BTC is not a pair
        self.assertEqual(keys("MACBETH and WEBTC"), set())

    def test_shouting_and_lookalike_tickers(self):
        self.assertEqual(keys("MUMU MAY NOT BE HUMAN BUT HES SHOWING SOMETHING DAMN NEAR HEART"), set())
        self.assertEqual(keys("I'M ALL IN ON $SOL RIGHT NOW BOYS LETS GO"), {"solana"})  # cashtags still count
        self.assertEqual(keys("dividends on STRF, STRC, STRK and STRD"), set())
        self.assertEqual(keys("LINK and AVAX look strong"), {"chainlink", "avalanche-2"})

    def test_stablecoins_dropped(self):
        self.assertEqual(keys("100,000,000 $USDC minted, swapped USDT for Tether"), set())

    def test_bare_symbols_and_stoplist(self):
        self.assertEqual(keys("LINK and AVAX look strong"), {"chainlink", "avalanche-2"})
        self.assertEqual(keys("THE SEC ETF news is NOT FUD, ONE day"), set())

    def test_lowercase_tickers_and_names(self):
        self.assertEqual(keys("long eth, short doge"), {"ethereum", "dogecoin"})
        self.assertEqual(keys("Bitcoin Cash is not Bitcoin"), {"bitcoin-cash", "bitcoin"})
        self.assertEqual(keys("bought some dogwifhat"), {"dogwifcoin"})

    def test_hype_the_word_is_not_a_coin(self):
        self.assertEqual(keys("so much hype around this rally"), set())
        self.assertEqual(keys("$HYPE and HYPE"), {"hyperliquid"})

    def test_common_word_names_ignored(self):
        self.assertEqual(keys("a stellar performance from the render farm"), set())

    def test_contract_addresses(self):
        evm = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"
        sol = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
        self.assertEqual(keys(f"CA: {evm}"), {f"ca:{evm.lower()}"})
        self.assertEqual(keys(f"new gem {sol} dyor"), {f"ca:{sol}"})
        # long ordinary words are not Solana addresses
        self.assertEqual(keys("supercalifragilisticexpialidociousness"), set())

    def test_cashtag_wins_over_symbol(self):
        (m,) = EXTRACTOR.extract("$SOL SOL Solana")
        self.assertEqual((m.key, m.method), ("solana", "cashtag"))


class SentimentTest(unittest.TestCase):
    def test_direction(self):
        self.assertGreater(sentiment.score("this gem is going to the moon 🚀🚀 wagmi"), 0.5)
        self.assertLess(sentiment.score("total rug, going to zero, avoid 💀"), -0.5)
        self.assertEqual(sentiment.score("the weather is mild"), 0.0)

    def test_negation(self):
        self.assertLess(sentiment.score("this is not bullish"), 0)


TELEGRAM_HTML = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="alpha_calls/101">
  <div class="tgme_widget_message_bubble">
   <div class="tgme_widget_message_text js-message_text" dir="auto">New call: <b>$FOO</b><br/>CA below
     <i class="emoji"><b>🚀</b></i></div>
   <div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/alpha_calls/101">
     <time datetime="2026-09-24T10:00:00+00:00" class="time">10:00</time></a></div>
  </div>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="alpha_calls/102">
  <div class="tgme_widget_message_bubble">
   <div class="tgme_widget_message_photo_wrap"></div>
   <div class="tgme_widget_message_footer"><time datetime="2026-09-24T10:05:00+00:00" class="time">10:05</time></div>
  </div>
 </div>
</div>
"""


class SourceParsingTest(unittest.TestCase):
    def test_telegram_preview(self):
        (post,) = telegram.parse_preview(TELEGRAM_HTML, "alpha_calls")  # photo-only msg skipped
        self.assertEqual(post.id, "telegram:alpha_calls/101")
        self.assertIn("$FOO", post.text)
        self.assertIn("🚀", post.text)
        self.assertNotIn("10:00", post.text)  # footer isn't message text
        self.assertEqual(post.url, "https://t.me/alpha_calls/101")

    def test_reddit_item(self):
        post = reddit.parse_listing_item(
            {"name": "t1_abc", "body": "$PEPE ripping", "author": "bob",
             "created_utc": 1700000000, "permalink": "/r/x/comments/1/_/abc"}, "CryptoCurrency")
        self.assertEqual((post.id, post.channel, post.author), ("reddit:t1_abc", "r/CryptoCurrency", "bob"))
        self.assertIsNone(reddit.parse_listing_item({"name": "t1_d", "body": "x", "author": "[deleted]"}, "a"))

    def test_reddit_rss(self):
        xml = """<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><author><name>/u/alice</name></author>
            <content type="html">&lt;p&gt;$PEPE looks strong&lt;/p&gt; submitted by /u/alice [link] [comments]</content>
            <id>t3_aaa</id><link href="https://www.reddit.com/r/x/comments/aaa/t/"/>
            <published>2026-09-24T03:00:00+00:00</published><title>SOL or ETH?</title></entry>
          <entry><author><name>/u/bob</name></author>
            <content type="html">&lt;div&gt;buying DOGE&lt;/div&gt;</content>
            <id>t1_bbb</id><updated>2026-09-24T03:05:00+00:00</updated>
            <title>/u/bob on SOL or ETH?</title></entry>
          <entry><author><name>/u/AutoModerator</name></author><content>rules</content><id>t1_c</id></entry>
        </feed>"""
        post, comment = reddit.parse_rss(xml, "CryptoCurrency")
        self.assertEqual((post.id, post.author), ("reddit:t3_aaa", "alice"))
        self.assertEqual(post.text, "SOL or ETH? $PEPE looks strong")
        self.assertEqual(comment.text, "buying DOGE")  # post title not credited to commenter
        self.assertGreater(comment.created_utc, 0)

    def test_fourchan_catalog(self):
        pages = [{"threads": [
            {"no": 1, "sticky": 1, "com": "rules"},
            {"no": 5, "time": 1700000000, "sub": "SOL thread", "com": "&gt;&gt;4 wagmi<br>$SOL",
             "last_replies": [{"no": 6, "time": 1700000100, "com": "<a class=\"quotelink\">&gt;&gt;5</a> ngmi"}]},
        ]}]
        posts = fourchan.parse_catalog(pages, "biz")
        self.assertEqual([p.id for p in posts], ["4chan:biz:5", "4chan:biz:6"])
        self.assertEqual(posts[0].text, "SOL thread  wagmi\n$SOL")
        self.assertEqual(posts[1].author, "thread:5")

    def test_rss(self):
        xml = """<rss><channel><item><title>Solana ETF approved</title>
                 <link>https://example.com/a</link><description>&lt;p&gt;Big news&lt;/p&gt;</description>
                 <pubDate>Wed, 24 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
        (post,) = news.parse_feed(xml, "https://www.example.com/rss")
        self.assertEqual(post.channel, "example.com")
        self.assertIn("Big news", post.text)
        self.assertGreater(post.created_utc, 0)

    def test_x_response(self):
        data = {"data": [{"id": "9", "text": "$BTC up", "author_id": "1",
                          "created_at": "2026-09-24T10:00:00.000Z"}],
                "includes": {"users": [{"id": "1", "username": "alice"}]}}
        (post,) = x.parse_response(data, "q")
        self.assertEqual((post.author, post.url), ("alice", "https://x.com/alice/status/9"))

    def test_dexscreener_picks_most_liquid_pair(self):
        pairs = [
            {"baseToken": {"address": "AbC", "symbol": "foo", "name": "Foo"}, "chainId": "solana",
             "liquidity": {"usd": 1000}, "url": "u1"},
            {"baseToken": {"address": "AbC", "symbol": "foo", "name": "Foo"}, "chainId": "solana",
             "liquidity": {"usd": 50000}, "url": "u2", "priceChange": {"h24": 120}},
        ]
        info = dexscreener.best_pairs(pairs)["abc"]
        self.assertEqual((info["symbol"], info["url"], info["price_change_24h"]), ("FOO", "u2", 120))


GOOGLE_RSS = """<?xml version="1.0"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
 <item><title>solana price</title><ht:approx_traffic>2000+</ht:approx_traffic>
   <ht:news_item><ht:news_item_title>Solana jumps 12%</ht:news_item_title></ht:news_item></item>
 <item><title>dogecoin</title><ht:approx_traffic>5000+</ht:approx_traffic></item>
 <item><title>link</title><ht:approx_traffic>1000+</ht:approx_traffic>
   <ht:news_item><ht:news_item_title>Zelda sequel trailer</ht:news_item_title></ht:news_item></item>
 <item><title>premier league</title><ht:approx_traffic>50000+</ht:approx_traffic></item>
</channel></rss>"""


class SearchTest(unittest.TestCase):
    def test_markets_prices(self):
        rows = [{"id": "phala", "current_price": 0.1, "price_change_percentage_1h_in_currency": 1.5,
                 "price_change_percentage_24h_in_currency": 53.0, "market_cap": 1e8}]
        self.assertEqual(search.parse_markets(rows)["phala"]["change_24h"], 53.0)
        trending = search.parse_coingecko_trending({"coins": [{"item": {
            "id": "phala", "symbol": "pha", "name": "Phala",
            "data": {"price": "$0.1", "price_change_percentage_24h": {"usd": 12.5}}}}]})
        self.assertEqual((trending[0]["price"], trending[0]["change_24h"]), (0.1, 12.5))

    def test_coingecko_trending(self):
        data = {"coins": [{"item": {"id": "pepe", "symbol": "pepe", "name": "Pepe", "market_cap_rank": 30}},
                          {"item": {"id": "tether", "symbol": "usdt", "name": "Tether"}},
                          {"item": {"id": "newcoin", "symbol": "new", "name": "NewCoin"}}]}
        rows = search.parse_coingecko_trending(data)
        self.assertEqual([(r["coin_key"], r["rank"]) for r in rows], [("pepe", 1), ("newcoin", 3)])

    def test_google_trends_matching(self):
        trends = search.parse_google_trends(GOOGLE_RSS)
        self.assertEqual(trends[0]["traffic"], "2000+")
        m = search.GoogleMatcher(coins.fallback_registry())
        matched = [getattr(m.match(t["query"], t["news"]), "id", None) for t in trends]
        # distinctive name matches alone; bare "link" with no crypto context does not
        self.assertEqual(matched, ["solana", "dogecoin", None, None])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.now = time.time()
        self.n = 0

    def post(self, text, author, hours_ago, source="reddit", channel="r/test"):
        self.n += 1
        p = Post(id=f"t:{self.n}", source=source, channel=channel, author=author,
                 created_utc=self.now - hours_ago * 3600, text=text)
        self.store.add_post(p, sentiment.score(text), EXTRACTOR.extract(text))

    def test_rankings(self):
        # Bitcoin: steady chatter, 2 people every hour for 3 days -> big but not rising.
        for h in range(0, 78):
            self.post("BTC", f"a{h % 7}", h + 0.5)
            self.post("bitcoin", f"b{h % 5}", h + 0.2)
        # PEPE: quiet in baseline, then 8 different people in the last 6h across two platforms.
        self.post("$PEPE", "early", 40)
        for i in range(8):
            self.post("$PEPE to the moon 🚀", f"p{i}", 0.5 + i * 0.5,
                      source="telegram" if i % 2 else "reddit")
        # Spam: one account posts $SCAMX 50 times in an hour -> just 1 voice.
        for _ in range(50):
            self.post("$SCAMX 1000x gem", "spammer", 1.2)
        # Brand-new unlisted coin, mentioned by 3 people.
        for i in range(3):
            self.post("$NEWCOIN", f"n{i}", 2)

        rep = report.build(self.store, window_h=6, baseline_h=72, now=self.now)
        by_key = {s.key: s for s in rep.stats}

        self.assertEqual(rep.most_talked(1)[0].key, "bitcoin")
        self.assertEqual(rep.heating_up(1)[0].key, "pepe")
        self.assertGreater(by_key["pepe"].sentiment, 0.3)
        self.assertEqual(by_key["pepe"].sources, {"reddit", "telegram"})
        self.assertLess(by_key["bitcoin"].velocity, 1.5)
        self.assertEqual(by_key["$SCAMX"].voices, 1)
        self.assertNotIn("$SCAMX", [s.key for s in rep.heating_up()])
        self.assertIn("$NEWCOIN", [s.key for s in rep.new_on_radar()])
        self.assertNotIn("pepe", [s.key for s in rep.new_on_radar()])  # seen 40h ago

        text = report.render_text(rep)
        self.assertIn("HEATING UP", text)
        self.assertIn("PEPE", text)

    def test_search_interest(self):
        h = 3600
        # PEPE: on CoinGecko trending for the last 3 snapshots only (entered ~1h ago)
        for t_ago, keys in [(5 * h, ["bitcoin"]), (2 * h, ["bitcoin"]),
                            (1 * h, ["bitcoin", "pepe"]), (0.2 * h, ["pepe", "bitcoin"])]:
            for rank, key in enumerate(keys, 1):
                self.store.add_search_trend(self.now - t_ago, "coingecko", key, rank, key.upper(), key)
        self.store.add_search_trend(self.now - 3 * h, "google-GB", "dogecoin", 4, "DOGE", "Dogecoin",
                                    '"dogecoin" 5000+')
        for i in range(6):
            self.post("$PEPE ripping", f"p{i}", 0.5)

        rep = report.build(self.store, window_h=6, baseline_h=72, now=self.now)
        by_key = {s.key: s for s in rep.stats}
        self.assertEqual(by_key["pepe"].search.cg_rank, 1)
        self.assertTrue(by_key["pepe"].search.entered_since(self.now - 6 * h))
        self.assertFalse(by_key["bitcoin"].search.entered_since(self.now - 6 * h))  # on list 5h+ (floor)
        self.assertEqual(by_key["dogecoin"].voices, 0)  # searched but not talked about
        self.assertNotIn("dogecoin", [s.key for s in rep.most_talked()])
        self.assertEqual([s.key for s in rep.search_interest()], ["pepe", "bitcoin", "dogecoin"])

        text = report.render_text(rep)
        self.assertIn("SEARCH INTEREST", text)
        self.assertIn("CG#1↑new", text)
        self.assertIn("GOOGLE", text)

    def test_early_search_signal_like_phala(self):
        h = 3600
        snaps = [(4 * h, {"bitcoin": 1, "pepe": 2}), (2.5 * h, {"bitcoin": 1, "pepe": 2}),
                 (1 * h, {"bitcoin": 1, "pepe": 2, "phala": 12}),
                 (0.2 * h, {"phala": 3, "bitcoin": 1, "pepe": 2})]
        for t_ago, ranks in snaps:
            for key, rank in ranks.items():
                self.store.add_search_trend(self.now - t_ago, "coingecko", key, rank, key.upper(), key)
        # PHALA: price flat when it appeared, starting to move now -> early
        self.store.add_price(self.now - 1 * h, "phala", 0.10, 0.5, 3.0, 1e8)
        self.store.add_price(self.now - 0.2 * h, "phala", 0.104, 2.0, 6.0, 1e8)
        # PEPE: climbing? no (flat at #2) ; already pumped 40%
        self.store.add_price(self.now - 0.2 * h, "pepe", 1.0, 1.0, 40.0, 1e9)

        rep = report.build(self.store, now=self.now)
        by_key = {s.key: s for s in rep.stats}
        ph = by_key["phala"].search
        self.assertEqual((ph.cg_rank, ph.rank_before, ph.climb), (3, None, 13))
        self.assertTrue(ph.early)
        self.assertAlmostEqual(ph.change_since_entry, 4.0, places=3)
        self.assertTrue(by_key["pepe"].search.already_pumped)
        self.assertFalse(by_key["pepe"].search.early)
        self.assertFalse(by_key["bitcoin"].search.early)  # no price data -> never "early"
        self.assertEqual([s.key for s in rep.search_signals()], ["phala"])

        text = report.render_text(rep)
        self.assertIn("EARLY SEARCH SIGNALS", text)
        self.assertIn("EARLY? climbing", text)
        self.assertIn("searched after a pump", text)
        self.assertIn("climbing in searches", report.render_search_alert(by_key["phala"]))

    def test_mid_spike_is_not_early_like_astro(self):
        h = 3600
        self.store.add_search_trend(self.now - 3 * h, "coingecko", "bitcoin", 1, "BTC", "Bitcoin")
        self.store.add_search_trend(self.now - 0.2 * h, "coingecko", "astro", 1, "ASTRO", "Astro")
        # Down 33% on the day but +138% in the last hour: mid-spike, not early.
        self.store.add_price(self.now - 0.2 * h, "astro", 0.05, 137.8, -33.0, 1e7)
        rep = report.build(self.store, now=self.now)
        astro = {s.key: s for s in rep.stats}["astro"].search
        self.assertTrue(astro.moving_now)
        self.assertFalse(astro.early)
        self.assertEqual(rep.search_signals(), [])
        self.assertIn("moving now: 1h +138%", report.render_text(rep))

    def test_mega_caps_are_never_early(self):
        h = 3600
        self.store.add_search_trend(self.now - 3 * h, "coingecko", "pepe", 1, "PEPE", "Pepe")
        self.store.add_search_trend(self.now - 0.2 * h, "coingecko", "bitcoin", 7, "BTC", "Bitcoin")
        self.store.add_price(self.now - 0.2 * h, "bitcoin", 100000.0, 0.0, 0.3, 2e12)
        rep = report.build(self.store, now=self.now)
        self.assertFalse({s.key: s for s in rep.stats}["bitcoin"].search.early)
        self.assertEqual(rep.search_signals(), [])

    def test_no_search_data_is_fine(self):
        rep = report.build(self.store, now=self.now)
        self.assertIn("(none right now)", report.render_text(rep))

    def test_pump_only_and_purge(self):
        for i in range(4):
            self.post("#FOO/USDT target 1 ✅", "degen", 0.5 + i, source="telegram",
                      channel="t.me/degenpump_crypto_pump_signals")
            self.post("$BAR looks good", f"r{i}", 0.5 + i)
            self.post("$BAR target hit", "degen", 0.5 + i, source="telegram",
                      channel="t.me/degenpump_crypto_pump_signals")
        pumps = frozenset({"t.me/degenpump_crypto_pump_signals"})
        rep = report.build(self.store, now=self.now, pump_channels=pumps)
        by_key = {s.key: s for s in rep.stats}
        self.assertTrue(by_key["$FOO"].pump_only)
        self.assertFalse(by_key["$BAR"].pump_only)  # also real people on Reddit
        self.assertNotIn("$FOO", [s.key for s in rep.heating_up() + rep.new_on_radar()])
        self.assertIn("ONLY IN SIGNAL/PUMP CHANNELS", report.render_text(rep))

        self.assertEqual(self.store.purge_channels(["t.me/degenpump_crypto_pump_signals"]), 8)
        self.assertNotIn("$FOO", [s.key for s in report.build(self.store, now=self.now).stats])

    def test_reextract_fixes_old_mentions(self):
        p = Post(id="old", source="4chan", channel="/biz/", author="a",
                 created_utc=self.now, text="DAMN NEAR HEART")
        self.store.add_post(p, 0, [Mention(key="near", symbol="NEAR", name="NEAR", method="symbol")])
        self.assertEqual(self.store.reextract(EXTRACTOR.extract), 1)
        self.assertEqual(self.store.mention_rows(0), [])

    def test_duplicate_posts_ignored_and_dead_contracts_hidden(self):
        p = Post(id="dup", source="x", channel="c", author="a", created_utc=self.now, text="$SOL")
        self.assertTrue(self.store.add_post(p, 0, EXTRACTOR.extract(p.text)))
        self.assertFalse(self.store.add_post(p, 0, EXTRACTOR.extract(p.text)))

        addr = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
        self.post(f"ca {addr}", "a", 0.1)
        self.assertEqual(self.store.unresolved_contracts(self.now - 3600), [f"ca:{addr}"])
        self.store.save_token_info(f"ca:{addr}", None)  # DEX Screener: not a token
        rep = report.build(self.store, now=self.now)
        self.assertNotIn(f"ca:{addr}", [s.key for s in rep.stats])
        self.assertEqual(self.store.find_keys("sol"), ["solana"])


if __name__ == "__main__":
    unittest.main()
