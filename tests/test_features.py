import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from trabe.api import ApiError, MarketData, message_chunks
from trabe.bot import COMMANDS, REMOVE_KEYBOARD, Bot
from trabe.config import Config
from trabe.outbox import Outbox
from trabe.preferences import validate_setting
from trabe.runtime import health, instance_lock, run
from trabe.scans import Scans
from trabe.store import Store

TOKEN = "123456:" + "a" * 32


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.store = Store(self.path / "agent.db")
        self.config = Config(TOKEN, 123, "key", self.store.path)
        self.calls = []

        def transport(url, **kwargs):
            from urllib.parse import parse_qs, urlparse

            ticker = parse_qs(urlparse(url).query)["symbol"][0]
            self.calls.append(ticker)
            return {
                "Global Quote": {
                    "01. symbol": ticker,
                    "05. price": "100",
                    "10. change percent": "-5%",
                    "07. latest trading day": "2026-10-30",
                }
            }

        self.market = MarketData("key", self.store, transport)
        self.telegram = FakeTelegram()
        self.bot = Bot(self.config, self.store, self.telegram, self.market)
        self.addCleanup(self.bot.scans.close)

    def test_start_removes_the_reply_keyboard(self):
        self.bot.handle(
            {
                "message": {
                    "text": "/start",
                    "from": {"id": 123},
                    "chat": {"id": 123, "type": "private"},
                }
            }
        )
        self.assertEqual(self.telegram.messages[0][2]["reply_markup"], REMOVE_KEYBOARD)
        self.assertEqual(REMOVE_KEYBOARD, {"remove_keyboard": True})
        self.assertEqual(self.calls, [])

    def test_button_is_owner_authorized_before_dispatch(self):
        for user in (999, 123):
            self.bot.handle(
                {
                    "message": {
                        "text": "Status",
                        "from": {"id": user},
                        "chat": {"id": 123, "type": "private"},
                    }
                }
            )
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertIn("running", self.telegram.messages[0][1])

    def test_cached_prices_never_use_network_even_if_expired(self):
        self.store.add("IBM", 1)
        self.store.add("MSFT", 2)
        self.market.quote("IBM")
        with self.store.connection() as db:
            db.execute("UPDATE quote_cache SET fetched_at=0")
        report = self.bot.command("prices", [])
        self.assertIn("2026-10-30", report)
        self.assertIn("MSFT: no cached quote", report)
        self.assertEqual(self.calls, ["IBM"])

    def test_scan_saves_recoverable_summary_and_uses_threshold(self):
        self.store.add("IBM", 1)
        result = self.bot.command("scan", [])
        self.assertIn("started", result)
        self.bot.scans._thread.join(timeout=3)
        self.assertEqual(self.store.latest_scan()[1], "completed")
        self.assertIn("dip flags: 1", self.bot.command("lastscan", []))
        self.assertIn("🔻 IBM", self.bot.scans.results.get_nowait())

    def test_scan_budget_stops_additional_provider_calls(self):
        self.store.set_setting("api_daily_budget", "1")
        self.store.add("IBM", 1)
        self.store.add("MSFT", 2)
        self.bot.scans.start()
        self.bot.scans._thread.join(timeout=3)
        summary = self.store.latest_scan()[2]
        self.assertIn("budget reached", summary)
        self.assertIn("Checked 1/2", summary)
        self.assertEqual(len(self.calls), 1)

    def test_running_scan_prevents_another_job(self):
        entered, release = threading.Event(), threading.Event()
        original = self.market.transport

        def blocked(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)

        self.market.transport = blocked
        self.store.add("IBM", 1)
        self.bot.scans.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertIn("already running", self.bot.scans.start())
            self.assertIn("Latest scan: #1 running", self.bot.command("status", []))
        finally:
            release.set()
            self.bot.scans._thread.join(timeout=3)
        self.assertEqual(self.store.latest_scan()[0], 1)

    def test_schedule_is_disabled_until_enabled(self):
        self.store.add("IBM", 1)
        self.bot.scans.schedule(datetime(2026, 11, 2, 23, tzinfo=UTC))
        self.assertIsNone(self.store.latest_scan())

    def test_scheduled_scan_runs_once_per_local_day_across_restart(self):
        self.store.add("IBM", 1)
        self.store.set_setting("daily_scan_time", "10:00")
        now = datetime(2026, 11, 2, 12, tzinfo=UTC)
        self.bot.scans.schedule(now)
        self.bot.scans._thread.join(timeout=3)
        other = Scans(Store(self.store.path), self.market)
        other.schedule(now)
        self.assertIsNone(other._thread)
        self.assertEqual(self.store.latest_scan()[0], 1)

    def test_schedule_skips_weekends_and_pre_schedule_time(self):
        self.store.add("IBM", 1)
        self.store.set_setting("daily_scan_time", "10:00")
        for now in (datetime(2026, 11, 1, 12, tzinfo=UTC), datetime(2026, 11, 2, 7, tzinfo=UTC)):
            self.bot.scans.schedule(now)
        self.assertIsNone(self.store.latest_scan())

    def test_schedule_uses_preference_timezone(self):
        self.store.add("IBM", 1)
        self.store.set_setting("timezone", "America/New_York")
        self.store.set_setting("daily_scan_time", "10:00")
        self.bot.scans.schedule(datetime(2026, 11, 2, 14, 30, tzinfo=UTC))
        self.assertIsNone(self.store.latest_scan())
        self.bot.scans.schedule(datetime(2026, 11, 2, 15, 1, tzinfo=UTC))
        self.bot.scans._thread.join(timeout=3)
        self.assertEqual(self.store.latest_scan()[0], 1)

    def test_scan_provider_outage_stops_after_three_failures(self):
        self.market.transport = lambda *a, **k: (_ for _ in ()).throw(ApiError("network"))
        for ticker in ("AAPL", "MSFT", "NVDA", "IBM"):
            self.store.add(ticker, 1)
        self.bot.scans.start()
        self.bot.scans._thread.join(timeout=3)
        self.assertEqual(self.store.usage(), 3)
        self.assertIn("remaining symbols skipped", self.store.latest_scan()[2])

    def test_market_cooldown_is_persistent_and_does_not_waste_budget(self):
        self.market.transport = lambda *a, **k: (_ for _ in ()).throw(ApiError("limit", 429, 120))
        with self.assertRaises(ApiError):
            self.market.quote("IBM")
        with self.assertRaisesRegex(ApiError, "cooling down"):
            MarketData("key", Store(self.store.path)).quote("MSFT")
        self.assertEqual(self.store.usage(), 1)

    def test_concurrent_quotes_share_one_cache_fill(self):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: self.market.quote("IBM"), range(4)))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(not cached for _, cached in results), 1)

    def test_online_backup_includes_uncheckpointed_data_and_is_private(self):
        self.store.add("IBM", 1)
        self.store.set_setting("timezone", "America/New_York")
        snapshot = self.path / "backup.db"
        self.store.backup(snapshot)
        other = Store(snapshot)
        self.assertEqual(other.watchlist(), [("IBM", 1)])
        self.assertEqual(other.settings()["timezone"], "America/New_York")
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)

    def test_backup_refuses_to_overwrite_existing_file(self):
        snapshot = self.path / "existing.db"
        snapshot.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            self.store.backup(snapshot)
        self.assertEqual(snapshot.read_bytes(), b"original")

    def test_newer_database_schema_is_refused(self):
        with self.store.connection() as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(ValueError, "newer"):
            Store(self.store.path)

    def test_duplicate_runtime_is_refused_before_startup(self):
        with instance_lock(self.store.path):
            with self.assertRaisesRegex(ValueError, "Another trabe"):
                with instance_lock(self.store.path):
                    self.fail("second process acquired the same lock")
        with instance_lock(self.store.path):
            pass

    def test_credentials_are_never_exposed_by_repr(self):
        self.assertNotIn(TOKEN, repr(self.config))
        self.assertNotIn("api_key=", repr(self.config))

    def test_outbox_survives_delivery_failure_and_process_restart(self):
        box = Outbox(self.store)
        box.send(123, "Saved IBM")
        with patch.object(self.telegram, "send", side_effect=ApiError("timeout")):
            with self.assertRaises(ApiError):
                box.flush(self.telegram)
        self.assertEqual(box.pending(), 1)
        reopened = Outbox(Store(self.store.path))
        reopened.flush(self.telegram)
        self.assertEqual(reopened.pending(), 0)
        self.assertEqual(self.telegram.messages[0][1], "Saved IBM")

    def test_outbox_acknowledges_each_chunk_and_does_not_replay_delivered_chunks(self):
        box = Outbox(self.store)
        text = "🚀" * 4000
        box.send(123, text, reply_markup=REMOVE_KEYBOARD)
        box.flush(self.telegram, limit=1)
        self.assertEqual(box.pending(), 2)
        self.assertEqual(self.telegram.messages[0][2]["reply_markup"], REMOVE_KEYBOARD)
        Outbox(Store(self.store.path)).flush(self.telegram)
        self.assertEqual("".join(message[1] for message in self.telegram.messages), text)
        self.assertNotIn("reply_markup", self.telegram.messages[-1][2])

    def test_outbox_is_bounded_and_expired_entries_are_removed(self):
        box = Outbox(self.store)
        for _ in range(500):
            box.send(123, "queued")
        with self.assertRaisesRegex(ValueError, "queue is full"):
            box.send(123, "one more")
        with self.store.connection() as db:
            db.execute("UPDATE outbox SET created_at=0")
        box.send(123, "new reply")
        self.assertEqual(box.pending(), 1)

    def test_expired_outbox_is_not_delivered_after_a_restart(self):
        box = Outbox(self.store)
        box.send(123, "obsolete price")
        with self.store.connection() as db:
            db.execute("UPDATE outbox SET created_at=0")
        Outbox(Store(self.store.path)).flush(self.telegram)
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(box.pending(), 0)

    def test_bad_symbol_separators_are_rejected_without_api_requests(self):
        for ticker in ("IBM.", "IBM..LON", "IBM--A", ".IBM"):
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                self.bot.command("add", [ticker])
        self.assertEqual(self.calls, [])

    def test_config_rejects_invalid_direct_construction(self):
        for owner in (0, -1, True, 2**52):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                Config(TOKEN, owner, "key", self.store.path)

    def test_daily_job_claim_is_atomic_across_workers(self):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=5) as workers:
            results = list(workers.map(lambda _: self.store.start_scan("2026-11-02"), range(10)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.store.finish_scan(next(result for result in results if result), "done")
        self.assertIsNone(self.store.start_scan("2026-11-02"))

    def test_new_preferences_are_validated_in_storage_layer(self):
        for key, value in (
            ("daily_scan_time", "25:00"),
            ("daily_scan_time", "9:00"),
            ("dip_threshold_percent", "NaN"),
            ("dip_threshold_percent", "4"),
            ("dip_threshold_percent", "-0"),
            ("dip_threshold_percent", "-51"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.store.set_setting(key, value)
        self.assertEqual(validate_setting("dip_threshold_percent", "-4.00"), "-4")

    def test_emoji_chunks_obey_telegram_utf16_limit(self):
        text = "🚀" * 4000 + "abc"
        chunks = list(message_chunks(text))
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks))

    def test_runtime_restores_signal_handlers_and_clears_health_on_exit(self):
        stop = threading.Event()

        api_calls = []

        class Client(FakeTelegram):
            def call(self, method, **kwargs):
                api_calls.append((method, kwargs))
                return {"username": "test_bot"} if method == "getMe" else {"url": ""}

            def updates(self, offset, **kwargs):
                stop.set()
                return []

        import signal

        old = signal.getsignal(signal.SIGTERM)
        run(self.config, telegram=Client(), stop_event=stop)
        self.assertIn(
            (
                "setMyCommands",
                {"commands": [{"command": c, "description": d} for c, d in COMMANDS]},
            ),
            api_calls,
        )
        self.assertIn(
            ("setChatMenuButton", {"chat_id": 123, "menu_button": {"type": "commands"}}), api_calls
        )
        self.assertIs(signal.getsignal(signal.SIGTERM), old)
        self.assertEqual(self.store.state("heartbeat"), "0")
        self.assertNotEqual(self.store.state("last_poll"), "")

    def test_readiness_requires_a_successful_poll_as_well_as_liveness(self):
        import os
        import time

        with patch.dict(os.environ, {"DATABASE_PATH": str(self.store.path)}):
            self.store.set_state("heartbeat", str(time.time()))
            self.assertTrue(health())
            self.assertFalse(health(require_poll=True))
            self.store.set_state("last_poll", str(time.time()))
            self.assertTrue(health(require_poll=True))
            self.store.set_state("heartbeat", "0")
            self.assertFalse(health(require_poll=True))


if __name__ == "__main__":
    unittest.main()
