"""Prospective comparison uses the issued briefing and its credited evidence."""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from trabe.config import Config
from trabe.insights import Insights
from trabe.news_event_store import (
    SUMMARY_SAMPLE_BYTES,
    SUMMARY_SAMPLE_LIMIT,
    TTL,
    EventStore,
    save_briefing_samples,
)
from trabe.outbox import Outbox
from trabe.store import Store


class NewsSamplingTests(unittest.TestCase):
    key = "daily:2026-11-18"
    headline = "NVIDIA announces CEO transition"
    body = (
        "NVIDIA (NASDAQ: NVDA) Board of Directors announced that its CEO "
        "has resigned, effective immediately."
    )

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "sample.db")
        self.now = datetime(2026, 11, 18, 12, tzinfo=UTC).timestamp()
        self.store.add("NVDA", 2)
        self.store.set_setting("briefing_enabled", "on")
        self.store.set_setting("briefing_time", "08:00")
        config = Config("100:" + "a" * 32, 123, "", self.store.path)
        self.box = Outbox(self.store)
        self.insights = Insights(config, self.store, self.box)
        self.repo = EventStore(self.store)
        for kind in ("earnings", "insiders", "news"):
            self.insights.repo.ok(kind, "fixture", "NVDA", self.now - 10)
        network = patch("trabe.signals.SignalHTTP._read", side_effect=AssertionError("network"))
        network.start()
        self.addCleanup(network.stop)

    def ingest(self):
        item = {
            "headline": self.headline,
            "snippet": self.body,
            "published_at": self.now - 120,
            "input_quality": "snippet",
            "provider": "fixture",
            "publisher": "Mirror",
            "item_id": "one",
            "url": "https://example.com/mirror",
        }
        self.repo.ingest([item], "NVDA", self.now - 90)
        self.repo.ingest(
            [
                item
                | {
                    "provider": "ir",
                    "item_id": "two",
                    "publisher": "Company IR",
                    "role": "issuer",
                    "url": "https://example.com/company",
                    "published_at": self.now - 100,
                }
            ],
            "NVDA",
            self.now - 80,
        )

    def test_issued_briefing_samples_actual_source_and_excerpts_once(self):
        self.ingest()
        with self.store.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            db.execute("DROP TABLE news_summary_samples")
        # Existing installed schema must acquire the additive table on normal open.
        restarted = Store(self.store.path)
        self.insights = Insights(self.insights.config, restarted, Outbox(restarted))
        with patch("trabe.insights.time.time", return_value=self.now):
            self.assertIn(self.body, self.insights.handle("briefing", ["cached"]))
        with restarted.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], version)
            self.assertEqual(db.execute("SELECT * FROM news_summary_samples").fetchall(), [])
        self.insights.scheduled_briefing(["NVDA"], self.now)
        self.insights.scheduled_briefing(["NVDA"], self.now + 60)
        self.assertEqual(self.box.pending(), 1)
        with restarted.connection() as db:
            records = db.execute("SELECT * FROM news_summary_samples").fetchall()
            queued = db.execute("SELECT text FROM outbox WHERE kind='briefing'").fetchone()[0]
        self.assertEqual(len(records), 1)
        key, position, as_of, ticker, raw = records[0]
        self.assertEqual((key, position, as_of, ticker), (self.key, 1, self.now, "NVDA"))
        sample = json.loads(raw)
        self.assertEqual(sample["source"]["publisher"], "Company IR")
        self.assertEqual(sample["source"]["published"], self.now - 100)
        self.assertEqual(sample["source"]["received"], self.now - 80)
        self.assertEqual(sample["source"]["payload"]["snippet"], self.body)
        self.assertEqual(sample["previous"]["excerpts"][0]["text"], self.headline)
        self.assertTrue(sample["previous"]["excerpts"][0]["in_credited_source"])
        self.assertEqual(sample["current"]["excerpts"][0]["text"], self.body)
        self.assertIn(sample["current"]["card"], queued)
        self.assertEqual(
            self.insights.repo.get("briefing", self.key)["payload"]["news_cards_displayed"], 1
        )
        with restarted.connection() as db:
            db.execute("DELETE FROM news_events")
        EventStore(restarted).prune(self.now + 86400)
        with restarted.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT * FROM news_summary_samples").fetchall(), records)

    def test_box_failure_rolls_back_sampling_and_empty_run_is_visible(self):
        self.ingest()
        with patch("trabe.outbox.Outbox.send", side_effect=ValueError("full")):
            with self.assertRaisesRegex(ValueError, "full"):
                self.insights.scheduled_briefing(["NVDA"], self.now)
        self.assertFalse(self.insights.repo.notified("briefing", self.key))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT * FROM news_summary_samples").fetchall(), [])
            db.execute("DELETE FROM news_events")
        self.insights.scheduled_briefing(["NVDA"], self.now)
        with self.store.connection() as db:
            result = db.execute(
                """SELECT n.event_key,json_extract(b.payload,'$.news_cards_displayed'),COUNT(s.position)
                FROM signal_notifications n
                LEFT JOIN signals b ON b.kind=n.kind AND b.event_key=n.event_key
                LEFT JOIN news_summary_samples s ON s.briefing_key=n.event_key
                WHERE n.kind='briefing' GROUP BY n.event_key"""
            ).fetchall()
        self.assertEqual(result, [(self.key, 0, 0)])

    def test_sample_caps_and_expiry(self):
        sample = {"source": {}, "ticker": "NVDA"}
        with self.store.connection() as db:
            for i in range(12):
                save_briefing_samples(db, f"daily:{i:02d}", self.now + i, [sample] * 4)
            save_briefing_samples(
                db,
                "oversized",
                self.now + 11,
                [sample | {"text": "🧪" * SUMMARY_SAMPLE_BYTES}],
            )
            count, max_per_run, first, last = db.execute(
                """SELECT SUM(n),MAX(n),MIN(briefing_key),MAX(briefing_key) FROM (
                SELECT briefing_key,COUNT(*) as n FROM news_summary_samples GROUP BY briefing_key)"""
            ).fetchone()
        self.assertEqual(
            (count, max_per_run, first, last), (SUMMARY_SAMPLE_LIMIT, 3, "daily:02", "daily:11")
        )
        self.repo.prune(self.now + TTL + 12)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM news_summary_samples").fetchone()[0], 0
            )

    def test_card_dropped_by_final_message_budget_is_not_sampled(self):
        self.ingest()
        with patch("trabe.market_context.MarketContext.briefing", return_value=["x" * 2850]):
            self.insights.scheduled_briefing(["NVDA"], self.now)
        run = self.insights.repo.get("briefing", self.key)["payload"]
        self.assertIn("Some sections exceed", run["text"])
        self.assertNotIn(self.body, run["text"])
        self.assertEqual(run["news_cards_displayed"], 0)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM news_summary_samples").fetchone()[0], 0
            )
