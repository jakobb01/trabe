"""Focused news normalization, provenance, fallback and bounded-cache checks."""

import tempfile
import unittest
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from trabe.api import ApiError
from trabe.ledger import Ledger
from trabe.news import News, canonical_url, summarize
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
            "headline": "Company raises guidance after strong earnings",
            "summary": "Revenue growth accelerated. The company raised guidance. A third sentence is omitted.",
            "datetime": self.now - offset,
            "url": url or f"https://publisher.example/news/{identity}?utm_source=feed",
            "related": symbol,
            "id": identity,
            "source": "Example Publisher",
            **overrides,
        }

    def rows(self):
        return self.news.repo.rows("news", limit=10000)

    def test_primary_extract_metadata_and_cached_views_spend_no_requests(self):
        self.http.data["GOOGL"] = [self.item()]
        result = self.news.refresh(["GOOGL"], now=self.now)
        self.assertEqual(result["items"], 1)
        row = self.rows()[0]
        self.assertEqual(row["available_at"], self.now)
        self.assertEqual(row["event_time"], self.now - 60)
        self.assertEqual(row["payload"]["sentiment"], "Bullish")
        self.assertNotIn("third sentence", row["payload"]["summary"])
        self.assertIn("publisher.example/news/1", row["payload"]["url"])
        self.assertNotIn("utm_", row["payload"]["url"])
        before = len(self.http.calls)
        self.news.refresh(["GOOGL"], now=self.now + 1)
        report = self.news.report(["GOOGL"], now=self.now + 2)
        self.news.briefing(["GOOGL"], now=self.now + 2)
        self.assertEqual(len(self.http.calls), before)
        self.assertIn("heuristic", report)
        self.assertIn("extractive-v1", report)
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
        first = self.item(url="https://publisher.example/story?article=12&utm_source=first")
        self.http.data["GOOGL"] = [first, first]
        self.news.refresh(["GOOGL"], now=self.now)
        self.http.data["VST"] = [
            self.item(
                symbol="VST",
                identity=2,
                url="https://publisher.example/story?utm_medium=email&article=12#top",
            )
        ]
        self.news.refresh(["VST"], now=self.now + 10)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observed_at"], self.now)
        self.assertEqual(rows[0]["available_at"], self.now)
        self.assertEqual(rows[0]["payload"]["symbols"], ["GOOGL", "VST"])
        self.assertIn("story?article=12", self.news.report(["VST"], now=self.now + 11))

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
        self.assertIn("yahoo-rss", report)
        self.assertIn("HTTP 403", report)
        self.assertNotIn("body must never", report)
        self.assertIn("First supplied sentence. Second supplied sentence.", report)
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
        self.assertIn("HTTP 429", report)
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
        self.http.data["GOOGL"] = [self.item()]
        self.news.refresh(["GOOGL"], now=self.now)
        daily = self.news.repo.get("news_daily", "GOOGL:2026-11-04")
        self.http.data["GOOGL"] = [self.item(summary="Company cuts guidance. Demand weakened.")]
        self.news.refresh(["GOOGL"], now=self.now + 10801)
        self.assertEqual(self.news.repo.get("news_daily", "GOOGL:2026-11-04"), daily)
        self.assertEqual(self.news._records(["GOOGL"], self.now + 1), [])
        self.assertIn("cuts guidance", self.news.report(["GOOGL"], now=self.now + 10802))

    def test_summary_bounds_headline_only_and_negation(self):
        headline = summarize("Issuer announces quarterly results", "")
        self.assertEqual(headline["summary_kind"], "headline-only")
        self.assertEqual(headline["sentiment"], "Neutral")
        self.assertNotIn(
            "<b>", summarize("Headline", "&lt;b&gt;A report.&lt;/b&gt; A second line.")["summary"]
        )
        self.assertEqual(summarize("Company did not raise guidance", "")["sentiment"], "Bearish")
        long = summarize("Headline", "A" * 2000 + ". " + "B" * 2000 + ".")
        self.assertLessEqual(len(long["summary"]), 475)
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
        self.assertEqual(len(self.http.calls), before)


if __name__ == "__main__":
    unittest.main()
