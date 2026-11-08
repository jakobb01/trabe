"""Bounded source parsing, independent quotas, authentication and cancellation."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from trabe.api import ApiError
from trabe.config import Config
from trabe.news import News, canonical_url, text
from trabe.news_sources import NewsSources
from trabe.signals import SignalHTTP, WorkStopped
from trabe.store import Store


class SourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "news.db")
        self.config = Config(
            "100:" + "a" * 32, 123, "", self.store.path, "", "massive-fixture", "marketaux-fixture"
        )
        self.now = 1793793600.0
        self.requests = []
        self.response = {}

        def opener(request, timeout):
            self.requests.append(request)
            return io.BytesIO(json.dumps(self.response).encode())

        self.http = SignalHTTP(self.config, self.store, opener=opener)
        self.http._pace = lambda *args: None
        self.news = News(self.store, self.http)

    def test_massive_labels_are_not_fabricated_numeric_scores_and_next_url_is_not_followed(self):
        self.response = {
            "results": [
                {
                    "id": "one",
                    "title": "Vistra raises guidance",
                    "description": "Vistra raised its outlook.",
                    "article_url": "https://example.com/story",
                    "published_utc": "2026-11-04T11:59:00Z",
                    "publisher": {"name": "Fixture"},
                    "tickers": ["VST", "GOOGL"],
                    "insights": [
                        {
                            "ticker": "VST",
                            "sentiment": "negative",
                            "sentiment_reasoning": "Weak demand",
                        },
                        {"ticker": "GOOGL", "sentiment": "positive"},
                    ],
                }
            ],
            "next_url": "https://unapproved.example/?apiKey=must-not-follow",
        }
        with patch("trabe.signals.time.time", return_value=self.now):
            items, capped = NewsSources(self.http, self.news._normalize, text).fetch(
                "massive", "VST", self.now
            )
        self.assertTrue(capped)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].get_header("Authorization"), "Bearer massive-fixture")
        self.assertNotIn("massive-fixture", self.requests[0].full_url)
        self.assertEqual(len(items[0]["annotations"]), 1)
        self.assertIsNone(items[0]["annotations"][0]["bias"])
        self.news._merge(items, "VST", "massive", self.now)
        view, _ = self.news._compute("VST", self.now)
        self.assertEqual(view["second_view"]["state"], "differs")
        self.assertEqual(self.store.provider_usage("massive"), 1)
        self.assertEqual(self.store.provider_usage("finnhub"), 0)

    def test_marketaux_entity_matching_strength_is_not_sentiment_or_confidence(self):
        self.response = {
            "data": [
                {
                    "uuid": "one",
                    "title": "Vistra cuts guidance",
                    "snippet": "Vistra reports weak demand.",
                    "url": "https://example.com/story",
                    "published_at": "2026-11-04T11:59:00Z",
                    "source": "Fixture",
                    "entities": [
                        {
                            "symbol": "VST",
                            "sentiment_score": -0.4,
                            "match_score": 35.2,
                            "highlights": [{"highlight": "<em>Vistra</em> reports weak demand"}],
                        }
                    ],
                }
            ]
        }
        with patch("trabe.signals.time.time", return_value=self.now):
            items, capped = NewsSources(self.http, self.news._normalize, text).fetch(
                "marketaux", "VST", self.now
            )
        self.assertFalse(capped)
        annotation = items[0]["annotations"][0]
        self.assertEqual(annotation["bias"], -0.4)
        self.assertEqual(annotation["match_score"], 35.2)
        self.assertEqual(annotation["highlights"], ["Vistra reports weak demand"])
        self.assertIsNone(canonical_url("https://example.com/story?api_token=secret"))

    def test_budget_is_persistent_manual_calls_cannot_bypass_it_and_429_is_redacted(self):
        self.response = {"results": []}
        self.store.set_setting("massive_daily_budget", "1")
        self.http.news_api("massive", ticker="VST")
        with self.assertRaises(ApiError):
            self.http.news_api("massive", ticker="GOOGL")
        self.assertEqual(len(self.requests), 1)
        self.store.set_setting("massive_daily_budget", "10")

        def unavailable(request, timeout):
            raise HTTPError(
                request.full_url,
                429,
                "secret provider body",
                {"Retry-After": "90"},
                io.BytesIO(b"private"),
            )

        self.http.opener = unavailable
        with self.assertRaises(ApiError) as caught:
            self.http.news_api("massive", ticker="VST")
        self.assertNotIn("secret", str(caught.exception))
        self.assertGreater(float(self.store.state("massive_retry_after")), 0)

    def test_snapshot_preparation_respects_enable_pause_deadline_and_setting_changes(self):
        self.store.set_setting("snapshot_enabled", "on")
        http = SignalHTTP(
            self.config,
            self.store,
            automatic=True,
            feature="snapshot",
            deadline=self.now + 10,
            guard_settings={"snapshot_time": "preopen-30m"},
            opener=self.http.opener,
        )
        http._pace = lambda *args: None
        with patch("trabe.signals.time.time", return_value=self.now + 11):
            with self.assertRaises(WorkStopped):
                http.news_api("massive", ticker="VST")
        self.store.set_setting("snapshot_time", "08:00")
        with patch("trabe.signals.time.time", return_value=self.now):
            with self.assertRaises(WorkStopped):
                http.news_api("massive", ticker="VST")
        self.assertEqual(self.requests, [])

    def test_missing_optional_key_preserves_local_news_and_reports_configuration(self):
        self.store.set_setting("news_sources", "massive")
        http = SignalHTTP(Config("100:" + "a" * 32, 123, "", self.store.path), self.store)
        news = News(self.store, http)
        self.assertEqual(news.refresh(["VST"], now=self.now)["items"], 0)
        self.assertIn("key missing", news.sources_status(["VST"]))
        self.assertIn("No clear signal", news.report(["VST"], now=self.now))


if __name__ == "__main__":
    unittest.main()
