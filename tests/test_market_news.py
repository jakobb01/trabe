"""Broad-market selection, supported article extracts and explicit feed gaps."""

import tempfile
import unittest
from email.utils import formatdate
from pathlib import Path

from test_earnings_news import NOW, article

from trabe.api import ApiError
from trabe.market_news import MarketNews
from trabe.signals import SignalStore, WorkStopped
from trabe.store import Store


class MarketNewsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "agent.db")
        self.calls = []
        self.rows = [self.item()]
        self.fail = False
        fixture = self

        class HTTP:
            def finnhub(self, endpoint, **kwargs):
                fixture.calls.append((endpoint, kwargs))
                if fixture.fail:
                    raise ApiError("private vendor error", 403)
                return fixture.rows

            def xml(self, url, provider):
                fixture.calls.append((provider, url))
                if "feeds.finance.yahoo.com" in url:
                    return f"<rss><channel><item><title>Inflation cools as Treasury yields fall</title><pubDate>{formatdate(NOW - 60, usegmt=True)}</pubDate><description>Consumer prices rose less than expected.</description><link>https://example.com/macro</link></item></channel></rss>".encode()
                return article(
                    "The Federal Reserve discussed interest rates and inflation. Treasury yields fell after the decision.",
                    headline="Federal Reserve discusses interest rates",
                    published="2026-11-07T02:12:31Z",
                )

        self.http = HTTP()
        self.feature = MarketNews(self.store, self.http)

    def item(self, **changes):
        return {
            "id": 1,
            "headline": "Federal Reserve discusses interest rates",
            "summary": "Treasury yields fell as investors assessed inflation.",
            "url": "https://example.com/fed",
            "source": "Reuters",
            "datetime": NOW - 60,
        } | changes

    def test_macro_selection_excludes_company_promotion_deduplicates_and_bounds(self):
        self.rows = [
            self.item(),
            self.item(
                id=2,
                headline="Federal Reserve discusses interest rates - Reuters",
                url="https://other.example/fed",
            ),
            self.item(id=3, headline="Amazon opens a warehouse", summary="New warehouse opened."),
            self.item(
                id=4,
                headline="Prediction: Best AI stock of 2027",
                summary="Nasdaq shares are rising.",
            ),
            self.item(id=5, headline="CPI data released tomorrow", datetime=NOW + 60),
            self.item(
                id=6,
                headline="US Treasury sanctions foreign money services",
                summary="Agency announced a sanctions action.",
                url="https://example.com/agency",
            ),
        ]
        self.feature.refresh(now=NOW)
        row = SignalStore(self.store).get("market-news", "latest")
        self.assertEqual(len(row["payload"]["items"]), 1)
        text = "\n".join(self.feature.briefing(now=NOW))
        self.assertEqual(text, "• Treasury yields fell as investors assessed inflation. • 1m ago")
        self.assertNotIn("Federal Reserve discusses", text)
        self.assertNotIn("Reuters", text)
        self.assertNotIn("Amazon", text)
        self.assertNotIn("Best AI", text)
        self.assertEqual(SignalStore(self.store).rows("news"), [])
        before = len(self.calls)
        self.feature.refresh(now=NOW + 1)
        self.assertEqual(len(self.calls), before)

    def test_primary_failure_uses_verified_index_rss_without_inventing_sentiment(self):
        self.fail = True
        self.feature.refresh(now=NOW)
        text = "\n".join(self.feature.briefing(now=NOW))
        self.assertIn("Consumer prices rose less than expected.", text)
        self.assertNotIn("Yahoo RSS fallback", text)
        self.assertEqual(
            SignalStore(self.store).get("market-news", "latest")["payload"]["source"], "yahoo-rss"
        )
        self.assertNotIn("Bullish", text)
        self.assertNotIn("private vendor", text)

    def test_supported_public_article_has_extract_quality_and_receipt_provenance(self):
        self.rows = [self.item(url="https://finance.yahoo.com/news/fed-decision.html")]
        self.feature.refresh(now=NOW)
        payload = SignalStore(self.store).get("market-news", "latest")["payload"]
        self.assertEqual(payload["items"][0]["input_quality"], "article")
        self.assertEqual(payload["feed_received_at"], NOW)
        self.assertEqual(payload["received_at"], NOW)
        text = "\n".join(self.feature.briefing(now=NOW))
        self.assertIn("The Federal Reserve discussed interest rates and inflation.", text)
        self.assertNotIn("Article extract:", text)

    def test_outage_keeps_saved_extract_age_and_failure_provenance(self):
        self.feature.refresh(now=NOW)

        def offline(*a, **kw):
            raise OSError("private network data")

        self.http.xml = self.http.finnhub = offline
        self.feature.refresh(now=NOW + 4 * 3600, force=True)
        text = "\n".join(MarketNews(Store(self.store.path), self.http).briefing(now=NOW + 4 * 3600))
        self.assertIn("Treasury yields fell", text)
        self.assertIn("4h ago", text)
        self.assertTrue(any(s["error"] for s in SignalStore(self.store).status("market-news")))
        self.assertNotIn("failed/deferred", text)
        self.assertNotIn("private network", text)

    def test_sources_off_and_cancellation_make_no_hidden_fallback_requests(self):
        self.store.set_setting("news_sources", "massive")
        self.feature.refresh(now=NOW)
        self.assertEqual(self.calls, [])
        self.store.set_setting("news_sources", "finnhub,yahoo")

        def stop(*a, **kw):
            raise WorkStopped("paused")

        self.http.finnhub = stop
        with self.assertRaises(WorkStopped):
            self.feature.refresh(now=NOW)
        self.assertFalse(any(s["error"] for s in SignalStore(self.store).status("market-news")))


if __name__ == "__main__":
    unittest.main()
