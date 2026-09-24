"""Offline tests: parsing fixtures, extraction, sentiment and scoring. Run from repo root:

    python -m unittest discover crypto_radar/tests
"""

import time
import unittest

from crypto_radar import coins, dexscreener, report, sentiment
from crypto_radar.extract import Extractor
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

    def test_bare_symbols_and_stoplist(self):
        self.assertEqual(keys("LINK and AVAX look strong"), {"chainlink", "avalanche-2"})
        self.assertEqual(keys("THE SEC ETF news is NOT FUD, ONE day"), set())

    def test_lowercase_tickers_and_names(self):
        self.assertEqual(keys("long eth, short doge"), {"ethereum", "dogecoin"})
        self.assertEqual(keys("Bitcoin Cash is not Bitcoin"), {"bitcoin-cash", "bitcoin"})
        self.assertEqual(keys("bought some dogwifhat"), {"dogwifcoin"})

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
