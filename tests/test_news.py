"""Focused news normalization, provenance, fallback and bounded-cache checks."""

import tempfile
import unittest
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from trabe.api import ApiError
from trabe.ledger import Ledger
from trabe.news import News, canonical_url, text
from trabe.signals import WorkStopped
from trabe.store import Store


class FakeHTTP:
    def __init__(self):
        self.data = {}
        self.primary_error = None
        self.rss_error = None
        self.rss = b"<rss><channel/></rss>"
        self.calls = []

    def finnhub(self, endpoint, **params):
        self.calls.append((endpoint, params))
        if self.primary_error:
            raise self.primary_error
        return self.data.get(params["symbol"], [])

    def xml(self, url, provider):
        self.calls.append((provider, url))
        if self.rss_error:
            raise self.rss_error
        return self.rss


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "news.db")
        self.http = FakeHTTP()
        self.news = News(self.store, self.http)
        self.now = datetime(2026, 11, 4, 12, tzinfo=UTC).timestamp()

    def item(self, *, symbol="GOOGL", identity=1, offset=60, url=None, **overrides):
        return {
            "headline": "Alphabet raises guidance after strong earnings",
            "summary": "Revenue growth accelerated. The company raised guidance. A third sentence is omitted.",
            "datetime": self.now - offset,
            "url": url or f"https://publisher.example/news/{identity}?utm_source=feed",
            "related": symbol,
            "id": identity,
            "source": "Example Publisher",
            **overrides,
        }

    def rows(self):
        return self.news._records(now=self.now + 20000, limit=10000)

    def test_primary_extract_metadata_and_cached_views_spend_no_requests(self):
        self.http.data["GOOGL"] = [self.item()]
        result = self.news.refresh(["GOOGL"], now=self.now)
        self.assertEqual(result["items"], 1)
        row = self.rows()[0]
        self.assertEqual(row["available_at"], self.now)
        self.assertEqual(row["event_time"], self.now - 60)
        self.assertEqual(row["payload"]["sentiment"], "Bullish")
        self.assertIn("third sentence", row["payload"]["snippet"].lower())
        self.assertIn("publisher.example/news/1", row["payload"]["url"])
        self.assertNotIn("utm_", row["payload"]["url"])
        before = len(self.http.calls)
        self.news.refresh(["GOOGL"], now=self.now + 1)
        report = self.news.report(["GOOGL"], now=self.now + 2)
        self.news.briefing(["GOOGL"], now=self.now + 2)
        self.assertEqual(len(self.http.calls), before)
        self.assertIn("heuristic", report)
        self.assertNotIn("extractive-v1", report)
        self.assertEqual(row["payload"]["input_quality"], "snippet")
        self.assertIn("Bullish", report)
        self.assertNotIn("https://", report)
        self.assertEqual(self.news.alerts(["GOOGL"], now=self.now), [])

    def test_invalid_stale_future_and_unrelated_items_are_rejected(self):
        self.http.data["GOOGL"] = [
            self.item(identity=1, datetime=float("nan")),
            self.item(identity=2, datetime=True),
            self.item(identity=3, datetime=self.now + 60),
            self.item(identity=4, offset=8 * 86400),
            self.item(identity=5, url="javascript:alert(1)"),
            self.item(identity=6, related="VST"),
            self.item(identity=7, url="https://user:password@example.com/a"),
            self.item(identity=8, url="https://example.com/a?token=credential"),
        ]
        self.news.refresh(["GOOGL"], now=self.now)
        self.assertEqual(self.rows(), [])

    def test_canonical_duplicate_across_symbols_and_source_ids_keeps_first_seen(self):
        first = self.item(
            url="https://publisher.example/story?article=12&utm_source=first",
            headline="Alphabet and Vistra raise guidance",
        )
        self.http.data["GOOGL"] = [first, first]
        self.news.refresh(["GOOGL"], now=self.now)
        self.http.data["VST"] = [
            self.item(
                symbol="VST",
                identity=2,
                headline="Alphabet and Vistra raise guidance",
                url="https://publisher.example/story?utm_medium=email&article=12#top",
            )
        ]
        self.news.refresh(["VST"], now=self.now + 10)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observed_at"], self.now)
        self.assertEqual(rows[0]["available_at"], self.now)
        self.assertEqual(rows[0]["payload"]["symbols"], ["GOOGL", "VST"])
        self.assertIn("story?article=12", rows[0]["payload"]["url"])
        self.assertIn("VST • Bullish", self.news.report(["VST"], now=self.now + 11))

    def test_rss_fallback_retains_primary_failure_and_extract_provenance(self):
        self.http.primary_error = ApiError("body must never be displayed", 403)
        stamp = format_datetime(datetime.fromtimestamp(self.now - 60, UTC))
        self.http.rss = f"""<rss><channel><item><title>Issuer announces an update</title>
        <link>https://publisher.example/rss-story?utm_source=yahoo</link>
        <guid>rss-1</guid><pubDate>{stamp}</pubDate>
        <description><![CDATA[<b>First supplied sentence.</b> Second supplied sentence.]]></description>
        </item></channel></rss>""".encode()
        self.news.refresh(["GOOGL"], now=self.now)
        report = self.news.report(["GOOGL"], now=self.now)
        self.assertIn("fallback feed", report)
        self.assertIn("HTTP 403", self.news._health("GOOGL")["primary_error"])
        self.assertNotIn("body must never", report)
        self.assertIn(
            "First supplied sentence. Second supplied sentence.",
            self.rows()[0]["payload"]["summary"],
        )
        self.assertNotIn("https://", report)
        self.assertNotIn("<b>", report)

    def test_source_failure_keeps_cache_and_reports_unavailability(self):
        self.http.data["GOOGL"] = [self.item()]
        self.news.refresh(["GOOGL"], now=self.now)
        self.http.primary_error = ApiError("credential-bearing diagnostic", 429)
        self.http.rss_error = OSError("offline")
        self.news.refresh(["GOOGL"], now=self.now + 10801)
        self.assertEqual(len(self.rows()), 1)
        report = self.news.report(["GOOGL"], now=self.now + 10802)
        self.assertIn("cached news retained", report)
        self.assertIn("HTTP 429", self.news._health("GOOGL")["primary_error"])
        self.assertNotIn("credential-bearing", report)

    def test_work_stopped_does_not_turn_pause_into_a_fallback_request(self):
        self.http.primary_error = WorkStopped("Paused")
        with self.assertRaises(WorkStopped):
            self.news.refresh(["GOOGL"], now=self.now)
        self.assertEqual(len(self.http.calls), 1)

    def test_paced_batch_uses_each_response_receipt_time(self):
        def paced(endpoint, **params):
            self.http.received_at = self.now + (10 if params["symbol"] == "GOOGL" else 20)
            return [
                self.item(
                    symbol=params["symbol"],
                    identity=params["symbol"],
                    datetime=self.http.received_at - 1,
                )
            ]

        self.http.finnhub = paced
        self.news.refresh(["GOOGL", "VST"], now=self.now)
        rows = {row["symbol"]: row for row in self.rows()}
        self.assertEqual(rows["GOOGL"]["observed_at"], self.now + 10)
        self.assertEqual(rows["VST"]["observed_at"], self.now + 20)
        self.assertEqual(rows["VST"]["available_at"], self.now + 20)
        self.assertEqual(self.news._health("VST")["last_success"], self.now + 20)

    def test_rss_uses_fallback_response_receipt_time(self):
        self.http.primary_error = ApiError("denied", 403)

        def delayed_rss(url, provider):
            self.http.received_at = self.now + 20
            stamp = format_datetime(datetime.fromtimestamp(self.now + 19, UTC))
            return f"<rss><channel><item><title>Fresh fallback</title><link>https://publisher.example/fresh</link><pubDate>{stamp}</pubDate></item></channel></rss>".encode()

        self.http.xml = delayed_rss
        self.news.refresh(["GOOGL"], now=self.now)
        row = self.rows()[0]
        self.assertEqual(row["observed_at"], self.now + 20)
        self.assertEqual(row["payload"]["first_seen_at"], self.now + 20)
        self.assertEqual(self.news._health("GOOGL")["checked_at"], self.now + 20)

    def test_retention_caps_prune_rows_and_keep_per_symbol_bounds(self):
        with patch("trabe.news.PER_SYMBOL", 2), patch("trabe.news.GLOBAL_LIMIT", 3):
            for number, symbol in enumerate(("GOOGL", "VST", "IBM")):
                self.http.data[symbol] = [
                    self.item(symbol=symbol, identity=number * 10 + i, offset=number * 10 + i)
                    for i in range(4)
                ]
                self.news.refresh([symbol], now=self.now)
        rows = self.rows()
        self.assertLessEqual(len(rows), 3)
        for symbol in ("GOOGL", "VST", "IBM"):
            self.assertLessEqual(sum(symbol in row["payload"]["symbols"] for row in rows), 2)

    def test_frozen_daily_signal_and_newer_content_do_not_leak_into_earlier_view(self):
        self.news.repo.put(
            "news_daily", "GOOGL:2026-11-04", "GOOGL", "legacy", {"score": 0.5}, now=self.now
        )
        self.http.data["GOOGL"] = [self.item()]
        self.news.refresh(["GOOGL"], now=self.now)
        daily = self.news.repo.get("news_daily", "GOOGL:2026-11-04")
        self.http.data["GOOGL"] = [self.item(summary="Company cuts guidance. Demand weakened.")]
        self.news.refresh(["GOOGL"], now=self.now + 10801)
        self.assertEqual(self.news.repo.get("news_daily", "GOOGL:2026-11-04"), daily)
        earlier = self.news._records(["GOOGL"], self.now + 1)
        self.assertEqual(len(earlier), 1)
        self.assertNotIn("cuts guidance", earlier[0]["payload"]["snippet"])
        self.assertIn("cuts guidance", self.rows()[0]["payload"]["summary"])

    def test_compact_reports_prioritize_sentiment_without_links_or_method_details(self):
        examples = (
            ("GOOGL", "Alphabet raises guidance", "Bullish"),
            ("VST", "Vistra cuts guidance", "Bearish"),
            ("IBM", "IBM schedules annual meeting", "Neutral"),
        )
        for symbol, headline, _ in examples:
            self.http.data[symbol] = [
                self.item(
                    symbol=symbol,
                    identity=f"{symbol}-{i}",
                    offset=i + 1,
                    headline=headline,
                    summary="",
                )
                for i in range(5)
            ]
        self.news.refresh([e[0] for e in examples], now=self.now)
        report = self.news.report([e[0] for e in examples], now=self.now)
        for symbol, headline, label in examples:
            self.assertIn(f"{symbol} • {label}", report)
            self.assertEqual(report.count(headline), 1)
        for unwanted in ("https://", "Example Publisher", "lexicon-v1", "extractive-v1", "score"):
            self.assertNotIn(unwanted, report)
        self.assertLess(len(report), 800)
        self.assertEqual(self.news.report(["VST"], now=self.now).count("Vistra cuts guidance"), 1)
        briefing = "\n".join(self.news.briefing(["GOOGL", "VST", "IBM"], now=self.now))
        self.assertIn("Bullish", briefing)
        self.assertNotIn("https://", briefing)
        self.assertNotIn("lexicon-v1", briefing)
        # Compact presentation does not discard richer inputs for a later classifier.
        self.assertEqual(len(self.rows()), 15)
        self.assertIn("url", self.rows()[0]["payload"])

    def test_missing_news_is_unknown_not_neutral(self):
        report = self.news.report(["VST"], now=self.now)
        self.assertIn("no recent cached news", report.lower())
        self.assertNotIn("Neutral", report)

    def test_signed_bias_is_visible_in_reports_and_briefings(self):
        examples = (
            ("Vistra raises guidance", "Bullish (+0.5)"),
            ("Vistra raises guidance after record profit", "Bullish (+1.0)"),
            ("Vistra schedules annual meeting", "Neutral (0.0)"),
            ("Vistra cuts guidance", "Bearish (-0.5)"),
            ("Vistra cuts guidance after weak profit", "Bearish (-1.0)"),
        )
        for index, (headline, expected) in enumerate(examples):
            now = self.now + index
            self.http.data["VST"] = [
                self.item(
                    symbol="VST",
                    identity="current",
                    headline=headline,
                    summary="",
                    datetime=now - 1,
                )
            ]
            self.news.refresh(["VST"], now=now, force=True)
            self.assertIn(expected, self.news.report(["VST"], now=now))
            self.assertIn(expected, self.news.briefing(["VST"], now=now, limit=1)[0])

    def test_supplied_input_bounds_markup_and_url_normalization(self):
        self.assertEqual(text("&lt;b&gt;A report.&lt;/b&gt;"), "A report.")
        normalized = self.news._normalize(
            self.item(headline="VST " + "A" * 2000, summary="B" * 10000),
            "GOOGL",
            "finnhub",
            self.now,
        )
        self.assertLessEqual(len(normalized["headline"]), 512)
        self.assertLessEqual(len(normalized["snippet"]), 4096)
        self.assertEqual(
            canonical_url("https://EXAMPLE.com/story?id=3&utm_source=rss#top"),
            "https://example.com/story?id=3",
        )

    def test_briefing_prioritizes_held_stock_news_without_market_requests(self):
        Ledger(self.store).record("GOOGL", "buy", "1", "100", "EUR", "2026-11-03")
        self.http.data["GOOGL"] = [self.item(offset=600, headline="Held stock report")]
        self.http.data["VST"] = [
            self.item(symbol="VST", identity=2, offset=1, headline="Watched stock report")
        ]
        self.news.refresh(["GOOGL", "VST"], now=self.now)
        before = len(self.http.calls)
        lines = self.news.briefing(["VST", "GOOGL"], now=self.now, limit=1)
        self.assertIn("GOOGL", lines[0])
        report = self.news.report(["VST", "GOOGL"], now=self.now)
        self.assertLess(report.index("GOOGL"), report.index("VST"))
        self.assertEqual(len(self.http.calls), before)


if __name__ == "__main__":
    unittest.main()
