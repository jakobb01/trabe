"""Frozen money/evidence, one-transaction capture, scheduling and owner controls."""

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from test_news import FakeHTTP

from trabe.api import MarketData
from trabe.bot import Bot
from trabe.calendar import USCalendar
from trabe.config import Config
from trabe.insights import Insights
from trabe.ledger import Ledger
from trabe.news import News
from trabe.outbox import Outbox
from trabe.snapshots import SnapshotService
from trabe.store import Store


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "snapshot.db")
        self.store.add("VST", 2)
        self.store.set_setting("market_provider", "finnhub")
        self.service = SnapshotService(self.store)
        self.now = USCalendar().session(date(2026, 11, 4)).opens - 1800
        previous = USCalendar().previous(date(2026, 11, 4))
        self.quote = {
            "ticker": "VST",
            "provider": "finnhub",
            "feed": "fixture",
            "currency": "USD",
            "price": 120,
            "source_time": previous.closes - 1,
            "received_at": previous.closes + 30,
            "trading_day": previous.day,
        }
        self.store.save_live(self.quote)
        Ledger(self.store).record("VST", "buy", "5", "100", "EUR", "2026-11-03", fee="1")
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO fx_cache VALUES (?,?,?,?,?)",
                ("2026-11-04", "1.2", "2026-11-03", "Fixture FX", self.now - 10),
            )
        self.news = News(self.store, FakeHTTP())
        self.item = {
            "provider": "finnhub",
            "item_id": "one",
            "url": "https://example.com/story",
            "headline": "Vistra raises guidance",
            "snippet": "",
            "input_quality": "headline_only",
            "published_at": self.now - 120,
        }
        self.news._merge([self.item], "VST", "finnhub", self.now - 100)

    def test_capture_uses_one_connection_and_preserves_decimal_money_and_reference_kind(self):
        with patch.object(self.store, "connection", wraps=self.store.connection) as connections:
            packet = self.service.capture(now=self.now)
        self.assertEqual(connections.call_count, 1)
        row = packet["symbols"]["VST"]
        self.assertEqual(row["position"]["basis"], "501")
        self.assertEqual(row["position"]["value"], "500")
        self.assertEqual(row["position"]["profit"], "-1")
        self.assertEqual(row["valuation_price"]["kind"], "previous_session_reference")
        self.assertIn("unverified", " ".join(row["valuation_price"]["quality"]))
        self.assertEqual(packet, self.service.repo.get())

    def test_later_trades_fx_currency_and_news_do_not_change_saved_packet(self):
        packet = self.service.capture(now=self.now)
        Ledger(self.store).record("VST", "buy", "1", "200", "EUR", "2026-11-02")
        self.store.set_setting("default_currency", "USD")
        with self.store.connection() as db:
            db.execute("UPDATE fx_cache SET usd_per_eur='2.0'")
        self.news._merge(
            [self.item | {"headline": "Vistra cuts guidance"}], "VST", "finnhub", self.now + 10
        )
        self.assertEqual(self.service.repo.get(), packet)
        with self.store.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE decision_snapshots SET payload='{}'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT OR REPLACE INTO decision_snapshots SELECT * FROM decision_snapshots"
                )
        second = self.service.capture(now=self.now + 61)
        self.assertNotEqual(second["id"], packet["id"])
        self.assertEqual(packet["currency"], "EUR")
        self.assertEqual(second["currency"], "USD")

    def test_future_receipts_are_missing_instead_of_zero_values(self):
        self.store.save_live(self.quote | {"received_at": self.now + 10})
        with self.store.connection() as db:
            db.execute("UPDATE fx_cache SET retrieved_at=?", (self.now + 10,))
        packet = self.service.capture(now=self.now)
        self.assertIsNone(packet["symbols"]["VST"]["position"]["value"])
        self.assertIsNone(packet["symbols"]["VST"]["valuation_price"])
        self.assertIsNone(packet["fx"])

    def test_vendor_annotation_arriving_later_cannot_enter_saved_packet(self):
        old = self.service.capture(now=self.now)
        other = self.item | {
            "provider": "massive",
            "annotations": [
                {
                    "ticker": "VST",
                    "direction": "negative",
                    "bias": None,
                    "method": "massive-insights/unknown",
                    "basis": "vendor_context_unavailable",
                }
            ],
        }
        self.news._merge([other], "VST", "massive", self.now + 10)
        new = self.service.capture(now=self.now + 61)
        self.assertEqual(old["symbols"]["VST"]["news"]["second_view"]["matched"], 0)
        self.assertEqual(new["symbols"]["VST"]["news"]["second_view"]["matched"], 1)
        self.assertEqual(new["symbols"]["VST"]["news"]["second_view"]["state"], "differs")

    def test_scheduled_claim_is_unique_manual_capture_does_not_replace_it(self):
        self.store.set_setting("snapshot_enabled", "on")
        first = self.service.capture("scheduled", "2026-11-04", self.now, now=self.now)
        again = self.service.capture("scheduled", "2026-11-04", self.now, now=self.now + 1)
        self.assertEqual(first["id"], again["id"])
        manual = self.service.capture(now=self.now + 1)
        self.assertNotEqual(manual["id"], first["id"])
        self.assertEqual(self.service.repo.get(session_date="2026-11-04"), first)
        with self.assertRaises(ValueError):
            self.service.capture(now=self.now + 30)

    def test_preparation_once_late_window_and_missed_run(self):
        self.store.set_setting("snapshot_enabled", "on")
        calls = []

        class Queue:
            def queue(inner, kind, **kwargs):
                calls.append((kind, kwargs))
                return 1

        queue = Queue()
        self.service.schedule(queue, self.now - 599)
        self.service.schedule(queue, self.now - 300)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["gate"], "snapshot")
        self.assertEqual(calls[0][1]["deadline"], self.now)
        self.service.schedule(queue, self.now + 30)
        packet = self.service.repo.get(session_date="2026-11-04")
        self.assertEqual(packet["lateness_seconds"], 30)
        tomorrow = USCalendar().session(date(2026, 11, 5)).opens - 1800
        self.service.schedule(queue, tomorrow + 301)
        self.assertIsNone(self.service.repo.get(session_date="2026-11-05"))
        with self.store.connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT status FROM snapshot_runs WHERE session_date='2026-11-05'"
                ).fetchone()[0],
                "missed",
            )

    def test_calendar_dst_holiday_and_custom_local_time(self):
        calendar = USCalendar()
        self.assertIsNone(calendar.session(date(2026, 11, 26)))
        spring = calendar.session(date(2026, 3, 9))
        self.assertEqual(datetime.fromtimestamp(spring.opens - 1800, UTC).hour, 13)
        self.assertEqual(datetime.fromtimestamp(self.now, UTC).hour, 14)
        self.store.set_setting("snapshot_time", "08:00")
        before = datetime(2026, 11, 4, 6, 55, tzinfo=UTC).timestamp()
        day, stamp = self.service.next_session(before)
        self.assertEqual(day, "2026-11-04")
        self.assertEqual(datetime.fromtimestamp(stamp, UTC).hour, 7)

    def test_pause_cancels_preparation_but_not_manual_jobs_and_saved_reads_work(self):
        self.service.capture(now=self.now)
        self.store.set_setting("snapshot_enabled", "on")
        config = Config("100:" + "a" * 32, 123, "", self.store.path)
        insights = Insights(config, self.store, Outbox(self.store))
        insights.queue(
            "news",
            origin="automatic",
            gate="snapshot",
            deadline=self.now + 100,
            session="2026-11-04",
        )
        insights.queue("news")
        self.store.pause_automation(self.now + 1000)
        with self.store.connection() as db:
            self.assertEqual(
                [r[0] for r in db.execute("SELECT status FROM signal_jobs ORDER BY id")],
                ["cancelled", "pending"],
            )
        self.store.set_state("storage_paused", "fixture pressure")
        self.assertIn("Snapshot #", self.service.handle(["latest"]))
        with self.assertRaises(ValueError):
            self.service.capture(now=self.now + 61)

    def test_manual_retention_and_payload_failure_preserve_ledger(self):
        for index in range(34):
            self.service.capture(now=self.now + index * 61)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM decision_snapshots").fetchone()[0], 32
            )
        self.service.repo.prune(self.now + 8 * 86400)
        self.assertIsNone(self.service.repo.get())
        with patch("trabe.snapshots.MAX_PACKET", 500):
            with self.assertRaises(ValueError):
                self.service.capture(now=self.now + 8 * 86400)
        self.assertEqual(Ledger(self.store).positions("EUR")["VST"][0], 5)

    def test_owner_authorization_applies_to_snapshot_and_news_source_controls(self):
        config = Config("100:" + "a" * 32, 123, "", self.store.path)
        messages = []

        class Sender:
            def send(inner, *args, **kwargs):
                messages.append(args)

        bot = Bot(config, self.store, Sender(), MarketData("", self.store))
        for text in ("/snapshot now", "/set news_sources massive"):
            bot.handle(
                {
                    "message": {
                        "chat": {"id": 999, "type": "private"},
                        "from": {"id": 999},
                        "text": text,
                    }
                }
            )
        self.assertEqual(messages, [])
        self.assertIsNone(self.service.repo.get())
        self.assertEqual(self.store.settings()["news_sources"], "finnhub,yahoo")
        bot.handle(
            {
                "message": {
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 123},
                    "text": "/snapshot now",
                }
            }
        )
        self.assertIn("Snapshot #", messages[-1][1])
        bot.handle(
            {
                "message": {
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 123},
                    "text": "/news sources",
                }
            }
        )
        self.assertIn("News sources:", messages[-1][1])

    def test_automatic_retention_expires_old_inputs_and_preserves_new_ledger(self):
        other = Store(self.store.path.parent / "old-snapshot.db")
        other.add("VST", 2)
        other.set_setting("snapshot_enabled", "on")
        service = SnapshotService(other)
        old_time = USCalendar().session(date(2026, 8, 3)).opens - 1800
        service.capture("scheduled", "2026-08-03", old_time, now=old_time)
        Ledger(other).record("VST", "buy", "1", "100", "EUR", "2026-11-03")
        service.repo.prune(self.now)
        self.assertIsNone(service.repo.get())
        self.assertEqual(Ledger(other).positions("EUR")["VST"][0], 1)

    def test_runtime_schedule_takes_cutoff_after_transaction_starts(self):
        self.store.set_setting("snapshot_enabled", "on")
        with patch("trabe.snapshots.time.time", side_effect=[self.now, self.now + 2]):
            self.service.schedule(None)
        packet = self.service.repo.get(session_date="2026-11-04")
        self.assertEqual(packet["as_of"], self.now + 2)
        self.assertEqual(packet["scheduled_for"], self.now)

    def test_future_expected_earnings_are_allowed_but_later_actuals_are_not_backfilled(self):
        from trabe.signals import SignalStore

        repo = SignalStore(self.store)
        event_time = self.now + 2 * 86400
        repo.put(
            "earnings",
            "quarter",
            "VST",
            "fixture",
            {"date": "2026-11-06", "epsEstimate": "2.0", "epsActual": None, "reported": False},
            now=self.now - 10,
            event_time=event_time,
        )
        old = self.service.capture(now=self.now)
        repo.put(
            "earnings",
            "quarter",
            "VST",
            "fixture",
            {"date": "2026-11-06", "epsEstimate": "2.0", "epsActual": "2.5", "reported": True},
            now=self.now + 10,
            event_time=event_time,
        )
        latest = self.service.capture(now=self.now + 61)
        self.assertIsNone(old["symbols"]["VST"]["earnings"][0]["facts"]["epsActual"])
        self.assertEqual(latest["symbols"]["VST"]["earnings"][0]["facts"]["epsActual"], "2.5")

    def test_qualified_insider_after_hundred_unqualified_rows_is_captured(self):
        from trabe.signals import SignalStore

        repo = SignalStore(self.store)
        for index in range(101):
            repo.put(
                "insiders",
                f"purchase-{index}",
                "VST",
                "fixture",
                {
                    "value_usd": "100000",
                    "eligibility_errors": [] if index == 100 else ["role unavailable"],
                },
                now=self.now - 1,
                event_time=self.now - 100 - index,
            )
        packet = self.service.capture(now=self.now)
        facts = packet["symbols"]["VST"]["insiders"]
        self.assertEqual([row["key"] for row in facts], ["purchase-100"])

    def test_preparation_finishes_silently_and_is_not_requeued_for_same_session(self):
        self.store.set_setting("snapshot_enabled", "on")
        config = Config("100:" + "a" * 32, 123, "", self.store.path)
        box = Outbox(self.store)
        http = FakeHTTP()
        insights = Insights(config, self.store, box, http_factory=lambda *a, **kw: http)
        with patch("trabe.insights.time.time", return_value=self.now - 300):
            first = insights.queue(
                "news", origin="automatic", gate="snapshot", deadline=self.now, session="2026-11-04"
            )
            insights.process_job()
            again = insights.queue(
                "news", origin="automatic", gate="snapshot", deadline=self.now, session="2026-11-04"
            )
        self.assertEqual(first, again)
        self.assertEqual(box.pending(), 0)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT status FROM signal_jobs WHERE id=?", (first,)).fetchone()[0],
                "completed",
            )


if __name__ == "__main__":
    unittest.main()
