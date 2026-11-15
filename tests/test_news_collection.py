"""Cursor security, durable fixed intervals and budget isolation with fixture HTTP."""

import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from trabe.api import ApiError
from trabe.config import Config
from trabe.news_collection import EventCollector, cursor_from_url, parse_feed
from trabe.news_event_store import EventStore
from trabe.signals import SignalHTTP
from trabe.store import Store


class CollectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "news.db")
        self.now = datetime(2026, 11, 14, 12, tzinfo=UTC).timestamp()
        with patch("trabe.store.time.time", return_value=self.now - 100):
            self.store.set_setting("news_enabled", "on")
        self.store.add("NVDA", 2)
        self.http = Mock(spec=["news_api", "issuer", "received_at", "stop"])
        self.http.stop = None
        self.http.received_at = self.now
        self.collector = EventCollector(self.store, self.http)
        self.generation = int(self.store.state("news_event_generation"))

    def scan(self):
        with self.store.connection() as db:
            return json.loads(
                db.execute(
                    "SELECT payload FROM event_collection WHERE source='massive-scan' AND ticker='NVDA'"
                ).fetchone()[0]
            )

    def page(self, cursor=None):
        return {
            "results": [
                {
                    "id": "extra",
                    "title": "NVIDIA announces CEO transition",
                    "tickers": ["NVDA"],
                    "article_url": "https://example.com/extra",
                    "published_utc": datetime.fromtimestamp(self.now - 600, UTC).isoformat(),
                }
            ],
            **(
                {"next_url": "https://api.massive.com/v2/reference/news?cursor=" + cursor}
                if cursor
                else {}
            ),
        }

    def first(self):
        self.collector.first_page("NVDA", self.page("first"), self.now, self.generation)

    def test_cursor_allowlist_rejects_credentials_hosts_and_extra_parameters(self):
        self.assertEqual(
            cursor_from_url("https://api.massive.com/v2/reference/news?cursor=abc%3D"), "abc="
        )
        for url in (
            "http://api.massive.com/v2/reference/news?cursor=a",
            "https://api.massive.com.evil/v2/reference/news?cursor=a",
            "https://user@api.massive.com/v2/reference/news?cursor=a",
            "https://api.massive.com:444/v2/reference/news?cursor=a",
            "https://api.massive.com/v1/news?cursor=a",
            "https://api.massive.com/v2/reference/news?cursor=a&apiKey=secret",
            "https://api.massive.com/v2/reference/news?cursor=a&cursor=b",
            "https://api.massive.com/v2/reference/news?cursor=" + "a" * 1100,
        ):
            with self.subTest(url=url[:80]), self.assertRaises(ValueError):
                cursor_from_url(url)

    def test_unsafe_first_page_metadata_is_partial_without_blocking_legacy(self):
        self.collector.first_page(
            "NVDA",
            {"results": [], "next_url": "https://evil.example/?apiKey=secret"},
            self.now,
            self.generation,
        )
        self.assertEqual(self.scan()["state"], "truncated")
        self.assertNotIn("secret", json.dumps(self.scan()))

    def test_extra_page_recovers_known_omission_without_changing_legacy_inputs(self):
        from trabe.news import News

        self.store.set_setting("news_sources", "massive")
        self.generation = int(self.store.state("news_event_generation"))
        self.http.config = Config(
            "100:" + "a" * 32, 123, "", self.store.path, massive_key="fixture"
        )
        initial = self.page("first")
        initial["results"][0]["id"] = "preview"
        initial["results"][0]["title"] = "NVIDIA earnings preview: what to expect"
        initial["results"][0]["article_url"] = "https://example.com/preview"
        self.http.news_api.side_effect = [initial, self.page()]
        news = News(self.store, self.http)
        news.refresh(["NVDA"], now=self.now)
        self.assertEqual(self.collector.repo.events(["NVDA"], self.now)[0]["materiality"], "low")
        self.collector.continuation("NVDA", self.now + 61, self.generation)
        self.assertEqual(
            self.collector.repo.events(["NVDA"], self.now + 61)[0]["type"], "leadership"
        )
        self.assertTrue(
            all(
                "CEO transition" not in r["headline"]
                for r in news.news_store.records("NVDA", self.now + 61)
            )
        )

    def test_official_archived_feed_fixtures_are_primary_and_separate(self):
        for ticker in ("NVDA", "AMD", "VST"):
            body = (Path(__file__).parent / f"fixtures/issuer_{ticker.lower()}.xml").read_bytes()
            rows = parse_feed(body, ticker, self.now)
            self.assertTrue(rows)
            self.assertTrue(all(r["role"] == "issuer" for r in rows))
        self.http.issuer.return_value = (
            (Path(__file__).parent / "fixtures/issuer_nvda.xml").read_bytes(),
            {"etag": "fixture"},
        )
        self.collector.official("NVDA", self.now, self.generation)
        events = self.collector.repo.events(["NVDA"], self.now)
        self.assertEqual(events[0]["type"], "leadership")
        self.assertEqual(events[0]["attribution"], "company statement")
        self.collector.official("NVDA", self.now + 20, self.generation)
        self.assertEqual(self.http.issuer.call_count, 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM news_revisions").fetchone()[0], 0)

    def test_restart_cursor_commit_exhaustion_and_overlap(self):
        self.first()
        initial = self.scan()
        self.assertEqual(initial["watermark"], 0)
        self.http.news_api.side_effect = [self.page("second"), self.page()]
        EventCollector(Store(self.store.path), self.http).continuation(
            "NVDA", self.now + 61, self.generation
        )
        complete = self.scan()
        self.assertIsNone(complete["cursor"])
        self.assertEqual(complete["watermark"], initial["upper"])
        self.assertEqual(len(EventStore(self.store).events(["NVDA"], self.now + 61)), 1)
        self.collector.first_page("NVDA", self.page("later"), self.now + 3600, self.generation)
        self.assertEqual(self.scan()["lower"], initial["upper"] - 6 * 3600)
        self.assertEqual(self.scan()["upper"], self.now + 3600)

    def test_commit_failure_retries_same_cursor_and_no_half_evidence(self):
        self.first()
        self.http.news_api.return_value = self.page()
        with patch.object(self.collector.repo, "ingest", side_effect=RuntimeError("fixture crash")):
            self.collector.continuation("NVDA", self.now + 61, self.generation)
        self.assertEqual(self.scan()["cursor"], "first")
        self.assertEqual(self.collector.repo.events(["NVDA"], self.now + 62), [])
        self.collector.continuation("NVDA", self.now + 3600, self.generation)
        self.assertIsNone(self.scan()["cursor"])

    def test_disable_inflight_rejects_evidence_and_cursor_commit(self):
        self.first()

        def response(*args, **kwargs):
            self.store.set_setting("news_enabled", "off")
            self.store.set_setting("news_enabled", "on")
            return self.page()

        self.http.news_api.side_effect = response
        self.collector.continuation("NVDA", self.now + 61, self.generation)
        self.assertEqual(self.scan()["cursor"], "first")
        self.assertEqual(self.collector.repo.events(["NVDA"], self.now + 62), [])

    def test_cursor_expiry_restarts_same_interval_and_repeat_is_partial(self):
        self.first()
        original = self.scan()
        self.http.news_api.side_effect = ApiError("expired", 410)
        self.collector.continuation("NVDA", self.now + 61, self.generation)
        self.assertTrue(self.scan()["restart"])
        self.http.news_api.side_effect = None
        self.http.news_api.return_value = self.page()
        self.collector.continuation("NVDA", self.now + 3600, self.generation)
        params = self.http.news_api.call_args.kwargs
        self.assertEqual(
            params["published_utc.lte"], datetime.fromtimestamp(original["upper"], UTC).isoformat()
        )
        self.first()
        self.http.news_api.return_value = self.page("first")
        self.collector.continuation("NVDA", self.now + 7200, self.generation)
        self.assertEqual(self.scan()["state"], "truncated")

    def test_extra_pages_reserve_baseline_and_global_attempt_cap(self):
        for ticker in ("AMD", "AMZN", "GOOGL", "VST"):
            self.store.add(ticker, 2)
        config = Config("100:" + "a" * 32, 123, "", self.store.path, massive_key="fixture")
        http = SignalHTTP(
            config, self.store, opener=lambda *args, **kwargs: io.BytesIO(b'{"results":[]}')
        )
        self.store.set_setting("massive_daily_budget", "100")
        with patch.object(http, "_pace"):
            with self.assertRaises(ApiError):
                http.news_api("massive", _presentation=True, cursor="a", limit=100)
            self.assertEqual(self.store.provider_usage("massive"), 0)
            self.store.set_setting("massive_daily_budget", "200")
            self.store.set_state(
                "massive_extra_pages", json.dumps({"day": self.store.today(), "count": 39})
            )
            http.news_api("massive", _presentation=True, cursor="a", limit=100)
            with self.assertRaises(ApiError):
                http.news_api("massive", _presentation=True, cursor="b", limit=100)
            self.assertEqual(self.store.provider_usage("massive"), 1)

    def test_healthy_empty_is_distinct_from_unavailable_and_xml_entities_rejected(self):
        self.http.issuer.return_value = (b"<rss><channel/></rss>", {})
        self.collector.official("NVDA", self.now, self.generation)
        self.assertIn(
            "empty response", " ".join(self.collector.repo.coverage_lines(["NVDA"], self.now))
        )
        self.http.issuer.side_effect = ApiError("fixture failure", 503)
        self.collector.official("NVDA", self.now + 3601, self.generation)
        self.assertIn(
            "unavailable", " ".join(self.collector.repo.coverage_lines(["NVDA"], self.now + 3601))
        )
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>', "NVDA", self.now)
