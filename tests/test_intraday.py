"""0.3 invariants: source eligibility, transactional alerts and bounded history."""

import fcntl
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch

from trabe.alerts import Alerts, breach
from trabe.api import ApiError
from trabe.calendar import USCalendar, retention_cutoff
from trabe.config import Config
from trabe.controls import Controls
from trabe.maintenance import Maintenance, prune_backups
from trabe.monitor import Ingestion, Monitor
from trabe.outbox import Outbox
from trabe.providers.finnhub import Finnhub
from trabe.providers.router import MarketRouter
from trabe.setup import write_credentials
from trabe.store import Store


class IntradayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.store = Store(self.path / "agent.db")
        self.store.add("VST", 2)
        self.store.set_setting("market_provider", "finnhub")
        self.store.set_setting("monitoring", "on")
        self.config = Config("123:" + "a" * 32, 123, "alpha", self.store.path, "finnhub-test")
        self.calendar = USCalendar()
        self.session = self.calendar.session(date(2026, 11, 3))
        self.now = self.session.opens + 600
        self.baseline = {
            "previous_date": "2026-11-02",
            "previous_close": 100.0,
            "open_price": 100.0,
            "source_time": self.now,
            "accepted": True,
            "reason": "",
        }
        self.store.save_baseline("VST", self.session.day, self.baseline)
        self.engine = Ingestion(self.store, 123)
        self.engine.configure(self.store.settings(), {}, ["VST"])
        self.engine.register(
            "VST", self.session, self.store.baseline("VST", self.session.day), self.session.opens
        )

    def tick(self, price, offset=0, **overrides):
        return {"s": "VST", "p": price, "t": int((self.now + offset) * 1000), **overrides}

    def counts(self):
        with self.store.connection() as db:
            return {
                table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("alert_events", "outbox", "minute_observations", "daily_summaries")
            }

    def test_brief_breach_is_durable_before_minute_flush_and_survives_recovery(self):
        self.engine.observe(self.tick(100), now=self.now)
        self.engine.observe(self.tick(94, 1), now=self.now + 1)
        self.assertEqual(self.counts()["alert_events"], 1)
        self.assertEqual(self.counts()["minute_observations"], 1)
        self.engine.observe(self.tick(101, 2), now=self.now + 2)
        self.engine.flush(now=self.now + 61)
        maintenance = Maintenance(Store(self.store.path))
        self.assertTrue(maintenance.rollup(self.session.day, now=self.session.closes + 601))
        history = self.store.history("VST")[0]
        self.assertEqual(
            (
                history["observed_open"],
                history["observed_high"],
                history["observed_low"],
                history["observed_last"],
            ),
            (100, 101, 94, 101),
        )
        self.assertEqual(history["alert_count"], 1)
        self.assertEqual(self.counts()["minute_observations"], 0)
        self.assertFalse(maintenance.rollup(self.session.day, now=self.session.closes + 900))
        self.assertEqual(self.store.history("VST"), [history])

    def test_invalid_future_stale_and_outside_session_frames_do_not_alert(self):
        bad = [
            self.tick(float("nan")),
            self.tick(float("inf")),
            self.tick(-1),
            self.tick(True),
            self.tick(90, t="not-a-timestamp"),
            self.tick(90, 60),
            self.tick(90, -121),
            self.tick(90, s="UNKNOWN"),
            self.tick(90, t=int((self.session.opens - 1) * 1000)),
        ]
        for trade in bad:
            self.assertFalse(self.engine.observe(trade, now=self.now))
        self.assertEqual(self.counts()["alert_events"], 0)
        self.assertEqual(self.counts()["minute_observations"], 0)

    def test_duplicates_out_of_order_and_remove_readd_cannot_resend(self):
        self.assertTrue(self.engine.observe(self.tick(94), now=self.now))
        self.assertFalse(self.engine.observe(self.tick(94), now=self.now))
        self.assertFalse(self.engine.observe(self.tick(90, -1), now=self.now))
        self.store.remove("VST")
        self.store.add("VST", 2)
        self.store.set_alert_rule("VST", "-3")
        other = Ingestion(Store(self.store.path), 123)
        other.configure(self.store.settings(), {}, ["VST"])
        other.register("VST", self.session, self.store.baseline("VST", self.session.day), self.now)
        other.observe(self.tick(93, 2), now=self.now + 2)
        self.assertEqual(self.counts()["alert_events"], 1)
        self.assertEqual(self.counts()["outbox"], 1)

    def test_concurrent_event_claims_commit_only_one_notification(self):
        self.engine.observe(self.tick(100), now=self.now)
        self.engine.observe(self.tick(97, 1), now=self.now + 1)
        quote = self.engine.latest("VST")
        quote["price"], quote["change_percent"] = 94.0, -6.0
        bucket = dict(next(iter(self.engine.buckets.values())))
        bucket["observed_low"] = bucket["observed_last"] = 94.0
        baseline = self.store.baseline("VST", self.session.day)

        def attempt(_):
            return Alerts(Store(self.store.path), 123).evaluate(
                "VST", quote, bucket, baseline, now=self.now + 1
            )

        with ThreadPoolExecutor(max_workers=5) as workers:
            results = list(workers.map(attempt, range(5)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.counts()["outbox"], 1)

    def test_failed_enqueue_rolls_back_event_and_trigger_bucket(self):
        with patch("trabe.alerts.Outbox.send", side_effect=ValueError("queue full")):
            with self.assertRaises(ValueError):
                self.engine.observe(self.tick(90), now=self.now)
        self.assertEqual(self.counts()["alert_events"], 0)
        self.assertEqual(self.counts()["minute_observations"], 0)
        self.engine.observe(self.tick(91, 1), now=self.now + 1)
        self.assertEqual(self.counts()["alert_events"], 1)

    def test_pause_cancels_proactive_messages_but_keeps_control_reply(self):
        self.engine.observe(self.tick(90), now=self.now)
        box = Outbox(self.store)
        box.send(123, "control reply")
        self.store.pause_automation(self.now + 3600)
        with self.store.connection() as db:
            self.assertEqual(
                list(db.execute("SELECT text,kind FROM outbox")), [("control reply", "reply")]
            )
        self.engine.observe(self.tick(89, 1), now=self.now + 1)
        self.assertEqual(self.counts()["alert_events"], 1)

    def test_concurrent_pause_is_checked_inside_claim_transaction(self):
        original = self.store.alert_rule

        def pause_first(ticker):
            self.store.pause_automation(self.now + 3600)
            return original(ticker)

        with patch.object(self.store, "alert_rule", side_effect=pause_first):
            self.engine.observe(self.tick(90), now=self.now)
        self.assertEqual(self.counts()["alert_events"], 0)

    def test_disabled_and_unverified_baselines_do_not_generate_alerts(self):
        self.store.set_alert_rule("VST", "off")
        self.engine.observe(self.tick(90), now=self.now)
        self.assertEqual(self.counts()["alert_events"], 0)
        self.store.set_alert_rule("VST", "default")
        self.store.save_baseline(
            "VST", self.session.day, self.baseline | {"accepted": False, "reason": "split suspect"}
        )
        self.engine.observe(self.tick(89, 1), now=self.now + 1)
        self.assertEqual(self.counts()["alert_events"], 0)

    def test_mixed_feed_currency_baseline_and_storage_pause_cannot_alert(self):
        self.engine.observe(self.tick(100), now=self.now)
        quote = self.engine.latest("VST") | {"price": 90, "change_percent": -10}
        bucket = dict(next(iter(self.engine.buckets.values())))
        baseline = self.store.baseline("VST", self.session.day)
        for change in (
            {"feed": "end-of-day"},
            {"currency": "EUR"},
            {"provider": "alphavantage"},
            {"previous_close": 101},
            {"ticker": "GOOGL"},
        ):
            self.assertFalse(
                Alerts(self.store, 123).evaluate(
                    "VST", quote | change, bucket, baseline, now=self.now
                )
            )
        self.store.set_state("storage_paused", "low space")
        self.assertFalse(
            Alerts(self.store, 123).evaluate("VST", quote, bucket, baseline, now=self.now)
        )
        self.assertEqual(self.counts()["alert_events"], 0)

    def test_partial_and_no_data_sessions_remain_explicit(self):
        maintenance = Maintenance(self.store)
        maintenance.rollup(self.session.day, now=self.session.closes + 601)
        row = self.store.history("VST")[0]
        self.assertIsNone(row["observed_last"])
        self.assertEqual(row["observed_minutes"], 0)
        self.assertTrue(row["quality_flags"] & 2)

    def test_early_close_and_dst_follow_exchange_time(self):
        short = self.calendar.session(date(2026, 11, 27))
        self.assertEqual(short.minutes, 210)
        summer = self.calendar.session(date(2026, 10, 30))
        winter = self.calendar.session(date(2026, 11, 2))
        from datetime import UTC, datetime

        self.assertEqual(datetime.fromtimestamp(summer.opens, UTC).hour, 13)
        self.assertEqual(datetime.fromtimestamp(winter.opens, UTC).hour, 14)
        self.assertIsNone(self.calendar.session(date(2026, 11, 26)))
        with self.assertRaisesRegex(ValueError, "expired"):
            self.calendar.session(date(2029, 1, 2))

    def test_three_month_cutoff_clamps_leap_and_month_end(self):
        self.assertEqual(retention_cutoff(date(2024, 5, 31)), date(2024, 2, 29))
        self.assertEqual(retention_cutoff(date(2027, 5, 31)), date(2027, 2, 28))
        self.assertEqual(retention_cutoff(date(2026, 11, 3)), date(2026, 8, 3))

    def test_expiry_preserves_settings_watchlist_and_active_jobs(self):
        self.store.start_scan()
        with self.store.connection() as db:
            for day in ("2026-08-03", "2026-08-04"):
                db.execute("INSERT INTO daily_runs VALUES (?,NULL)", (day,))
                db.execute("INSERT INTO provider_usage VALUES ('finnhub',?,5)", (day,))
        Maintenance(self.store).expire(self.now)
        with self.store.connection() as db:
            self.assertEqual(list(db.execute("SELECT day FROM daily_runs")), [("2026-08-04",)])
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM scan_jobs WHERE status='running'").fetchone()[0], 1
            )
        self.assertEqual(self.store.watchlist(), [("VST", 2)])
        self.assertEqual(self.store.settings()["monitoring"], "on")

    def test_backup_pruning_keeps_protected_and_manual_files(self):
        folder = self.path / "backups"
        folder.mkdir()
        names = [f"pre-deploy-2026110{n}T120000Z-abcd.sqlite3" for n in range(1, 6)]
        for name in names:
            (folder / name).write_bytes(b"db")
        protected = "pre-deploy-20260101T000000Z-abcd.sqlite3"
        (folder / protected).write_bytes(b"protected")
        (folder / "manual.sqlite3").write_bytes(b"manual")
        (folder / ".protected.json").write_text(json.dumps({"name": protected}))
        result = prune_backups(folder, now=self.now + 10 * 86400)
        self.assertEqual(set(result["removed"]), set(names[:2]))
        self.assertTrue((folder / protected).exists())
        self.assertTrue((folder / "manual.sqlite3").exists())

    def test_storage_low_space_pauses_collection_without_deleting_state(self):
        maintenance = Maintenance(self.store, 123)
        metrics = maintenance.metrics() | {"free_bytes": 1}
        maintenance.guard(metrics)
        self.assertIn("Disk reserve", self.store.state("storage_paused"))
        self.assertEqual(self.store.watchlist(), [("VST", 2)])
        self.assertEqual(self.counts()["outbox"], 1)
        maintenance.guard(metrics)
        self.assertEqual(self.counts()["outbox"], 1)

    def test_busy_wal_reader_is_visible_and_checkpoint_recovers(self):
        reader = sqlite3.connect(self.store.path)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM settings").fetchall()
        self.store.set_state("writer_after_reader", "1")
        connect = sqlite3.connect

        def short_timeout(*args, **kwargs):
            return connect(*args, **(kwargs | {"timeout": 0.01}))

        maintenance = Maintenance(self.store)
        with patch("trabe.store.sqlite3.connect", side_effect=short_timeout):
            maintenance.run_once(now=self.now)
        self.assertIn("busy", self.store.state("maintenance_checkpoint"))
        reader.rollback()
        reader.close()
        maintenance.run_once(now=self.now)
        self.assertEqual(self.store.state("maintenance_checkpoint"), "complete")

    def test_finnhub_credential_round_trip_is_private(self):
        destination = self.path / "credentials"
        write_credentials(destination, self.config.token, 123, "alpha", "finnhub-private")
        with patch.dict(os.environ, {}, clear=True):
            restored = Config.load(destination)
        self.assertEqual(restored.finnhub_key, "finnhub-private")
        self.assertNotIn("finnhub-private", repr(restored))
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_stale_stream_cache_does_not_prevent_a_manual_rest_refresh(self):
        self.engine.observe(self.tick(100), now=self.now)
        router = MarketRouter(self.config, self.store)
        router.monitor = self.engine
        with patch("trabe.providers.router.time.time", return_value=self.now + 121):
            with patch.object(
                router.finnhub, "quote", return_value=({"fresh": True}, False)
            ) as fetch:
                self.assertEqual(router.quote("VST"), ({"fresh": True}, False))
                fetch.assert_called_once_with("VST", refresh=False)

    def test_pure_rule_rejects_bad_prices_and_checks_boundary(self):
        self.assertAlmostEqual(breach(96, 100, -4), -4)
        self.assertIsNone(breach(float("nan"), 100, -4))
        self.assertIsNone(breach(0, 100, -4))
        self.assertIsNone(breach(97, 100, -4))

    def test_normal_close_is_not_a_disconnect_gap(self):
        self.engine.gap(now=self.session.closes + 1)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT quality_flags FROM market_sessions").fetchone()[0], 0
            )
        self.engine.gap(now=self.now)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT quality_flags FROM market_sessions").fetchone()[0], 1
            )

    def test_finalized_session_rejects_late_minute_writes(self):
        self.engine.observe(self.tick(100), now=self.now)
        bucket = dict(next(iter(self.engine.buckets.values())))
        self.engine.flush(all_buckets=True)
        Maintenance(self.store).rollup(self.session.day, now=self.session.closes + 601)
        self.store.save_bucket(bucket)
        self.assertEqual(self.counts()["minute_observations"], 0)

    def test_rollup_failure_rolls_back_summary_and_minute_deletion(self):
        self.engine.observe(self.tick(100), now=self.now)
        self.engine.flush(all_buckets=True)
        with self.store.connection() as db:
            db.execute(
                "CREATE TRIGGER reject_finalize BEFORE UPDATE ON market_sessions BEGIN SELECT RAISE(ABORT,'fixture crash'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            Maintenance(self.store).rollup(self.session.day, now=self.session.closes + 601)
        self.assertEqual(self.counts()["minute_observations"], 1)
        self.assertEqual(self.counts()["daily_summaries"], 0)

    def test_backup_pruner_yields_to_deployment_lock(self):
        folder = self.path / "backups"
        folder.mkdir()
        with (folder / ".backup.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertTrue(prune_backups(folder, now=self.now)["busy"])

    def test_controls_validate_provider_rules_and_pause_persistence(self):
        router = MarketRouter(self.config, self.store)
        monitor = Monitor(self.store, self.config, router.finnhub)
        controls = Controls(self.config, self.store, router, monitor)
        self.store.set_state("finnhub_verified_key", router.finnhub.key_fingerprint)
        controls.handle("alert", ["VST", "-5"])
        self.assertEqual(self.store.alert_rule("VST"), (True, -5))
        controls.handle("pause", ["24h"])
        self.assertTrue(Store(self.store.path).automation_paused())
        self.assertTrue(monitor.engine.paused)
        controls.handle("resume", [])
        self.assertFalse(monitor.engine.paused)
        controls.handle("provider", ["alphavantage"])
        self.assertEqual(self.store.settings()["monitoring"], "off")
        with self.assertRaisesRegex(ValueError, "verify Finnhub"):
            controls.handle("monitor", ["on"])
        controls.handle("provider", ["finnhub"])
        self.assertEqual(self.store.settings()["monitoring"], "off")
        for command in ("status", "alerts", "storage", "history"):
            before = self.store.provider_usage("finnhub")
            if command == "status":
                controls.status()
            else:
                controls.handle(command, ["VST"] if command == "history" else [])
            self.assertEqual(self.store.provider_usage("finnhub"), before)

    def test_schema_two_upgrade_preserves_existing_state_and_pending_reply(self):
        path = self.path / "legacy.db"
        old = Store(path)
        old.add("GOOGL", 1)
        old.set_setting("daily_scan_time", "23:10")
        old.set_setting("quote_cache_minutes", "1440")
        old.reserve_request(20)
        Outbox(old).send(123, "pending old reply")
        # Real v2 has the original five outbox columns and none of v3's tables.
        with old.connection() as db:
            db.execute("ALTER TABLE outbox DROP COLUMN kind")
            for table in (
                "provider_usage",
                "provider_calls",
                "live_quotes",
                "alert_preferences",
                "instruments",
                "feeds",
                "market_sessions",
                "session_symbols",
                "baselines",
                "minute_observations",
                "daily_summaries",
                "alert_events",
            ):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=2")
        restored = Store(path)
        self.assertEqual(restored.watchlist(), [("GOOGL", 1)])
        self.assertEqual(restored.settings()["daily_scan_time"], "23:10")
        self.assertEqual(restored.settings()["quote_cache_minutes"], "1440")
        self.assertEqual(restored.settings()["monitoring"], "off")
        self.assertEqual(restored.usage(), 1)
        with restored.connection() as db:
            self.assertEqual(
                db.execute("SELECT text,kind FROM outbox").fetchall(),
                [("pending old reply", "reply")],
            )


class FinnhubTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "db.sqlite3")
        self.calls = []

        def transport(url, **kwargs):
            self.calls.append((url, kwargs))
            if "profile2" in url:
                return {
                    "ticker": "VST",
                    "currency": "USD",
                    "exchange": "NEW YORK STOCK EXCHANGE, INC.",
                }
            return {"c": 100, "pc": 100, "o": 100, "dp": 0, "t": 1793718000}

        self.client = Finnhub("secret-finnhub", self.store, transport)

    def test_rest_secret_uses_header_and_cache_does_not_spend_requests(self):
        with patch("trabe.providers.finnhub.time.sleep"):
            quote, cached = self.client.quote("VST")
            again, reused = self.client.quote("VST")
        self.assertFalse(cached)
        self.assertTrue(reused)
        self.assertEqual(quote, again)
        self.assertEqual(self.store.provider_usage("finnhub"), 2)
        self.assertEqual(self.store.usage(), 0)
        self.assertTrue(all("secret-finnhub" not in url for url, _ in self.calls))
        self.assertTrue(
            all(
                options["headers"]["X-Finnhub-Token"] == "secret-finnhub"
                for _, options in self.calls
            )
        )

    def test_forbidden_endpoint_is_classified_without_leaking_response(self):
        self.client.transport = lambda *a, **k: (_ for _ in ()).throw(
            ApiError("provider body secret-finnhub", 403)
        )
        with self.assertRaises(ApiError) as caught:
            self.client.quote("VST")
        self.assertEqual(caught.exception.code, 403)
        self.assertNotIn("secret-finnhub", str(caught.exception))

    def test_independent_persistent_minute_limit_reserves_attempts(self):
        for _ in range(45):
            self.store.reserve_provider("finnhub", 100)
        with self.assertRaisesRegex(ValueError, "minute allowance"):
            Store(self.store.path).reserve_provider("finnhub", 100)
        self.assertEqual(self.store.usage(), 0)

    def test_automatic_requests_stop_before_spending_quota_when_disabled(self):
        with self.assertRaisesRegex(ValueError, "paused"):
            self.client.quote("VST", automatic=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.provider_usage("finnhub"), 0)

    def test_suspicious_opening_gap_disables_baseline(self):
        session = USCalendar().session(date(2026, 11, 3))
        quote = {
            "trading_day": session.day,
            "source_time": session.opens + 60,
            "open_price": 25,
            "previous_close": 100,
        }
        with patch.object(self.client, "quote", return_value=(quote, False)):
            baseline = self.client.prepare_baseline("VST", session)
        self.assertFalse(baseline["accepted"])
        self.assertIn("review", baseline["reason"])


if __name__ == "__main__":
    unittest.main()
