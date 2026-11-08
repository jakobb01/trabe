"""News evidence must identify the actual quarter and EPS basis before display."""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from trabe.earnings import Earnings
from trabe.earnings_news import extract
from trabe.earnings_results import calendar_estimates
from trabe.signals import SignalStore
from trabe.store import Store

NOW = datetime(2026, 11, 8, 1, tzinfo=UTC).timestamp()


def article(
    text="Basic EPS (Q3 2026 vs Q3 2025): US$3.25 vs. US$1.78",
    *,
    headline="Vistra (VST) Q3 earnings",
    published="2026-11-07T02:12:31Z",
    extra="",
):
    meta = {"@type": "NewsArticle", "headline": headline, "datePublished": published}
    return (
        f'<script type="application/ld+json">{json.dumps(meta)}</script>{extra}<article>{text}</article>'
    ).encode()


class EarningsNewsTests(unittest.TestCase):
    def parse(self, body, *, period="FY2026 Q3"):
        return extract(body, "VST", period, "2026-11-06", NOW, ["Vistra"])

    def test_exact_current_quarter_is_basic_eps_not_a_consensus_comparison(self):
        result = self.parse(article())
        self.assertEqual(result["actual"], "3.25")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["basis"], "basic EPS reported in news")
        self.assertIsNone(result["estimate"])
        self.assertIsNone(self.parse(article(), period="FY2025 Q3"))

    def test_zero_and_explicit_usd_are_valid(self):
        self.assertEqual(
            self.parse(article("Basic EPS (Q3 2026 vs Q3 2025): USD 0 vs USD 1"))["actual"], "0"
        )
        self.assertEqual(
            self.parse(article("Diluted EPS (Q3 2026 vs Q3 2025): USD 3.22 vs USD 1"))["currency"],
            "USD",
        )

    def test_calendar_estimate_context_preserves_zero_missing_values_and_source(self):
        self.assertIsNone(calendar_estimates({}, "finnhub"))
        self.assertEqual(
            calendar_estimates({"epsEstimate": "0", "revenueEstimate": "0"}, "finnhub"),
            "Calendar estimates (Finnhub): EPS 0 • Revenue 0",
        )
        self.assertEqual(
            calendar_estimates({"epsEstimate": "-0.5"}),
            "Saved calendar estimates: EPS -0.5 • Revenue unavailable",
        )

    def test_prices_forecasts_unrelated_and_ambiguous_reports_are_rejected(self):
        for body in (
            article("Share price US$3.25; EPS expected to rise"),
            article("Basic EPS forecast (Q3 2026 vs Q3 2025): US$3.25"),
            article(headline="Another company earnings"),
            article(headline="Vistra (VST) preview ahead of Q3 earnings"),
            article(published="2026-11-10T00:00:00Z"),
            article(
                "Basic EPS (Q3 2026 vs Q3 2025): US$3.25; Basic EPS (Q3 2026 vs Q3 2025): US$1.75"
            ),
            article("Nothing reported", extra="<svg>Basic EPS (Q3 2026 vs Q3 2025): US$3.25</svg>"),
        ):
            with self.subTest(body=body):
                self.assertIsNone(self.parse(body))

    def test_service_corrects_withdrawn_result_and_deduplicates_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "app.db")
            store.add("VST", 2)
            store.set_setting("earnings_enabled", "on")
            repo = SignalStore(store)
            repo.put(
                "earnings",
                "event",
                "VST",
                "finnhub",
                {
                    "date": "2026-11-06",
                    "period": "FY2026 Q3",
                    "hour": "before-open",
                    "reported": False,
                    "comparisons": {},
                    "epsEstimate": "2.991",
                    "revenueEstimate": "6084287722",
                    "result_tracking": True,
                    "withdrawn_results": {"eps": {"actual": "1.75", "checked_at": NOW - 100}},
                },
                now=NOW - 100,
                event_time=NOW - 100,
            )

            class HTTP:
                def xml(self, url, **kwargs):
                    self.received_at = NOW + 5
                    if "feeds.finance.yahoo.com" in url:
                        return b"<rss><channel><item><title>Vistra (VST) Q3 earnings</title><pubDate>Sat, 07 Nov 2026 03:12:31 GMT</pubDate><link>https://finance.yahoo.com/news/blocked.html</link></item><item><title>Vistra (VST) Q3 earnings</title><pubDate>Sat, 07 Nov 2026 02:12:31 GMT</pubDate><link>https://finance.yahoo.com/news/example.html</link></item></channel></rss>"
                    if "blocked.html" in url:
                        raise OSError("private provider diagnostic")
                    return article()

            from trabe.earnings_news import EarningsNews

            EarningsNews(store, HTTP()).refresh("VST", now=NOW)
            saved = repo.get("earnings", "event")
            self.assertEqual(saved["last_seen"], NOW + 5)
            self.assertEqual(saved["payload"]["comparisons"]["eps"]["actual"], "3.25")
            service = Earnings(store, HTTP())
            report = service.report(["VST"], now=NOW + 5)
            self.assertIn("EPS (basic): 3.25 USD", report)
            expected = "Calendar estimates (Finnhub): EPS 2.991 • Revenue 6.08B"
            self.assertIn(expected, report)
            self.assertIn("Revenue actual: unavailable", report)
            self.assertNotIn("vs unavailable expected", report)
            self.assertNotIn("MISS", report)
            self.assertNotIn("HIT", report)
            self.assertIn(expected, "\n".join(service.briefing(["VST"], now=NOW + 5)))
            self.assertIsNone(
                repo.get("earnings", "event")["payload"]["comparisons"]["eps"]["estimate"]
            )
            alerts = list(service.results.alerts(["VST"], now=NOW + 5))
            self.assertEqual(len(alerts), 1)
            self.assertIn("Earnings correction", alerts[0][1])
            self.assertIn(expected, alerts[0][1])
            key, text = alerts[0]
            self.assertTrue(
                repo.notify_once(
                    123, "earnings", key, text, now=NOW + 5, setting="earnings_enabled"
                )
            )
            self.assertEqual(
                list(Earnings(Store(store.path), HTTP()).results.alerts(["VST"], now=NOW + 6)), []
            )
