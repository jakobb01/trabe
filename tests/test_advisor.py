"""Frozen company research, no future evidence, bounded storage and owner controls."""

import copy
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from test_news import FakeHTTP

from trabe.advisor import Advisor, digest
from trabe.bot import Bot
from trabe.config import Config
from trabe.ledger import Ledger
from trabe.maintenance import Maintenance
from trabe.news import News
from trabe.signals import SignalStore
from trabe.snapshots import SnapshotService
from trabe.store import Store


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "agent.db")
        self.store.add("VST", 2)
        self.now = stamp("2026-11-09T16:00:00+00:00")
        self.advisor = Advisor(self.store)
        self.news = News(self.store, FakeHTTP())
        self.signals = SignalStore(self.store)
        self.items = [
            {
                "provider": "finnhub",
                "item_id": str(i),
                "url": f"https://example.com/{i}",
                "headline": headline,
                "snippet": snippet,
                "input_quality": "snippet",
                "published_at": self.now - 300,
            }
            for i, (headline, snippet) in enumerate(
                [
                    ("Vistra raises guidance", "Vistra raises guidance after revenue growth."),
                    (
                        "Vistra wins regulatory approval",
                        "Vistra wins regulatory approval for its new facility.",
                    ),
                ]
            )
        ]
        self.seed(self.items)
        self.store.save_live(
            dict(
                ticker="VST",
                provider="finnhub",
                currency="USD",
                price=120,
                source_time=self.now - 20,
                received_at=self.now - 10,
            )
        )

    def seed(self, items, now=None):
        now = self.now - 60 if now is None else now
        self.news._merge(items, "VST", "finnhub", now)
        self.signals.ok("news", "finnhub", "VST", now)

    def test_five_complete_future_sessions_and_exact_frozen_inputs(self):
        with patch.object(self.store, "connection", wraps=self.store.connection) as connection:
            packet = self.advisor.issue("VST", self.now)
        self.assertEqual(connection.call_count, 1)
        self.assertEqual(
            packet["sessions"],
            ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"],
        )
        self.assertGreater(packet["opens"], self.now)
        self.assertEqual(packet["forecast"]["direction"], "bullish")
        self.assertEqual(packet["forecast"]["action"], "research adding")
        self.assertEqual(packet["inputs_sha256"], digest(packet["inputs"]))
        self.assertIsNone(packet["forecast"]["confidence"])
        self.assertEqual(len(packet["inputs"]["news"]), 2)

    def test_holidays_and_early_close_define_the_horizon(self):
        packet = self.advisor.issue("VST", stamp("2026-11-25T22:00:00+00:00"))
        self.assertEqual(
            packet["sessions"],
            ["2026-11-27", "2026-11-30", "2026-12-01", "2026-12-02", "2026-12-03"],
        )
        before = self.advisor.issue("VST", stamp("2026-11-20T14:00:00+00:00"))
        self.assertEqual(before["closes"], stamp("2026-11-27T18:00:00+00:00"))

    def test_later_news_and_restart_do_not_rewrite_or_reissue(self):
        packet = self.advisor.issue("VST", self.now)
        changed = [
            item | {"headline": "Vistra cuts guidance", "snippet": "Vistra cuts guidance."}
            for item in self.items
        ]
        self.seed(changed, self.now + 10)
        restarted = Advisor(Store(self.store.path))
        self.assertEqual(restarted.issue("VST", self.now + 20), packet)
        self.assertEqual(restarted.get("VST"), packet)
        with self.store.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE company_advice SET payload='{}'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT OR REPLACE INTO company_advice SELECT * FROM company_advice")

    def test_future_news_and_price_cannot_support_a_suggestion(self):
        for item in self.items:
            item["item_id"] += "future"
            item["url"] += "future"
        self.seed(self.items, self.now + 60)
        self.store.save_live(
            dict(
                ticker="VST",
                provider="finnhub",
                currency="USD",
                price=200,
                source_time=self.now + 10,
                received_at=self.now + 10,
            )
        )
        packet = self.advisor.issue("VST", self.now)
        self.assertIsNone(packet["inputs"]["price"])
        self.assertEqual(len(packet["inputs"]["news"]), 2)
        self.assertTrue(all(s["known_at"] <= self.now for s in packet["inputs"]["news"]))
        self.assertEqual(packet["forecast"]["direction"], "insufficient")

    def test_syndication_does_not_supply_two_independent_stories(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM news_articles")
        duplicates = [
            self.items[0],
            self.items[0] | {"item_id": "copy", "url": "https://copy.example/article"},
        ]
        self.seed(duplicates)
        packet = self.advisor.issue("VST", self.now)
        self.assertEqual(len(packet["inputs"]["news"]), 1)
        self.assertEqual(packet["forecast"]["direction"], "insufficient")

    def test_conflicting_stories_produce_mixed_not_confidence(self):
        negative = self.items[1] | {
            "headline": "Vistra cuts guidance",
            "snippet": "Vistra cuts guidance after weak demand.",
        }
        self.seed([negative], self.now - 30)
        packet = self.advisor.issue("VST", self.now)
        self.assertEqual(packet["forecast"]["direction"], "mixed")
        self.assertEqual(packet["forecast"]["action"], "hold/watch")

    def test_stale_headline_only_and_failed_source_abstain(self):
        base = self.advisor.issue("VST", self.now)["inputs"]
        for mode in ("headline", "stale", "failed"):
            inputs = copy.deepcopy(base)
            if mode == "headline":
                for item in inputs["news"]:
                    item["input_quality"] = "headline_only"
            elif mode == "stale":
                for source in inputs["news_sources"]:
                    source["succeeded_at"] = self.now - 20000
            else:
                inputs["news_sources"][0]["error"] = "failed"
            result = Advisor.infer(
                inputs, self.now, self.advisor.calendar.session(date(2026, 11, 16))
            )
            self.assertEqual(result["direction"], "insufficient", mode)
            self.assertEqual(result["action"], "wait for evidence", mode)

    def test_earnings_risk_defers_positive_action(self):
        self.signals.put(
            "earnings",
            "vst-q3",
            "VST",
            "fixture",
            {"date": "2026-11-12"},
            now=self.now - 10,
            event_time=self.now + 86400,
        )
        packet = self.advisor.issue("VST", self.now)
        self.assertEqual(packet["forecast"]["direction"], "bullish")
        self.assertEqual(packet["forecast"]["action"], "wait for earnings")

    def test_negative_action_depends_on_frozen_holding(self):
        inputs = self.advisor.issue("VST", self.now)["inputs"]
        for story in inputs["news"]:
            story["analysis"]["bias"] = -0.5
        last = self.advisor.calendar.session(date(2026, 11, 16))
        self.assertEqual(Advisor.infer(inputs, self.now, last)["action"], "avoid adding")
        inputs["position_quantity"] = "2"
        self.assertEqual(Advisor.infer(inputs, self.now, last)["action"], "review reducing")

    def test_packet_and_record_limits_do_not_delete_pending_advice(self):
        with patch("trabe.advisor.MAX_PACKET", 1):
            with self.assertRaisesRegex(ValueError, "packet limit"):
                self.advisor.issue("VST", self.now)
        self.assertIsNone(self.advisor.get("VST"))
        packet = self.advisor.issue("VST", self.now)
        self.store.add("GOOGL", 2)
        with patch("trabe.advisor.MAX_RECORDS", 1):
            with self.assertRaisesRegex(ValueError, "retention limit"):
                self.advisor.issue("GOOGL", self.now)
        self.assertEqual(self.advisor.get("VST"), packet)

    def test_retention_and_schema_upgrade_preserve_real_ledger(self):
        Ledger(self.store).record("VST", "buy", "2", "100", "USD", "2026-11-03")
        packet = self.advisor.issue("VST", self.now)
        with self.store.connection() as db:
            db.execute("PRAGMA user_version=8")
        Store(self.store.path)
        self.assertEqual(self.advisor.get("VST"), packet)
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
        Maintenance(self.store, 123).expire(stamp("2027-03-01T12:00:00+00:00"))
        self.assertIsNone(self.advisor.get("VST"))
        self.assertEqual(Ledger(self.store).positions("USD")["VST"][0], 2)

    def test_upgrade_adds_advisor_without_rewriting_existing_snapshot(self):
        old = SnapshotService(self.store).capture(now=self.now)
        with self.store.connection() as db:
            db.execute("DROP TABLE company_advice")
            db.execute("PRAGMA user_version=8")
        upgraded = Store(self.store.path)
        self.assertEqual(SnapshotService(upgraded).repo.get(), old)
        self.assertIsNone(Advisor(upgraded).get("VST"))
        self.assertEqual(
            Advisor(upgraded).issue("VST", self.now)["forecast"]["direction"], "bullish"
        )

    def test_unknown_symbol_storage_pause_and_expired_calendar_do_not_write(self):
        with self.assertRaisesRegex(ValueError, "watchlist"):
            self.advisor.issue("IBM", self.now)
        self.store.set_state("storage_paused", "full")
        with self.assertRaisesRegex(ValueError, "Storage"):
            self.advisor.issue("VST", self.now)
        with self.assertRaisesRegex(ValueError, "calendar"):
            self.advisor.issue("VST", stamp("2029-01-01T12:00:00+00:00"))
        self.assertIsNone(self.advisor.get("VST"))

    def test_owner_commands_and_saved_evidence_spend_no_provider_requests(self):
        telegram = Mock()
        config = Config("100:" + "a" * 32, 123, "", self.store.path)
        bot = Bot(config, self.store, telegram, Mock())
        self.addCleanup(bot.scans.close)
        for user, chat, kind in ((999, 123, "private"), (123, 999, "private"), (123, 123, "group")):
            bot.handle(
                {
                    "message": {
                        "text": "/advisor VST",
                        "from": {"id": user},
                        "chat": {"id": chat, "type": kind},
                    }
                }
            )
        self.assertIsNone(self.advisor.get("VST"))
        telegram.send.assert_not_called()
        with patch("trabe.advisor.time.time", return_value=self.now):
            text = bot.command("advisor", ["VST"])
        self.assertIn("bullish", text)
        self.assertIn("https://example.com/", bot.command("advisor", ["evidence", "VST"]))
        self.assertEqual(bot.command("advisor", ["cached", "VST"]), text)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM signal_jobs").fetchone()[0], 0)
        self.assertEqual(self.news.http.calls, [])


if __name__ == "__main__":
    unittest.main()
