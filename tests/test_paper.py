"""Daily scheduling, cache-only Telegram views, FX timing and guarded source work."""

import io
import json
import unittest
from datetime import date
from unittest.mock import Mock, patch

import test_advisor as advisor_fixtures
from test_paper_rules import inputs_for
from test_price_endpoints import chart

from trabe.bot import COMMANDS, Bot
from trabe.calendar import USCalendar
from trabe.config import Config
from trabe.fx import FX
from trabe.insights import Insights
from trabe.ledger import Ledger
from trabe.outbox import Outbox
from trabe.paper import Paper
from trabe.price_endpoints import EndpointStore, checked, parse_endpoint
from trabe.signals import WorkStopped
from trabe.store import Store


class PaperTests(unittest.TestCase):
    def setUp(self):
        advisor_fixtures.AdvisorTests.setUp(self)
        self.calendar = USCalendar()
        self.session = self.calendar.session(date(2026, 11, 10))
        self.current = self.session.opens - 900
        self.paper = Paper(self.store, clock=lambda: self.current)
        self.paper.handle(["start"])
        self.seed(self.items, self.current - 60)
        previous = self.calendar.previous(date.fromisoformat(self.session.day))
        value = parse_endpoint(
            chart("VST", previous, closing="50"),
            symbol="VST",
            session=previous,
            edge="close",
            received_at=previous.closes + 1800,
        )
        with self.store.connection() as db:
            EndpointStore(self.store).save(value, db=db)
            self.paper.repo.observe_fx(
                db, self.session.day, "1.10", self.session.day, "Frankfurter/ECB", self.current - 30
            )
        self.config = Config("100:" + "a" * 32, 123, "", self.store.path)
        self.insights = Insights(self.config, self.store, Outbox(self.store))

    def seed(self, *args, **kwargs):
        return advisor_fixtures.AdvisorTests.seed(self, *args, **kwargs)

    def saved(self):
        with self.store.connection() as db:
            return [
                checked(r)
                for r in db.execute("SELECT payload,sha256 FROM paper_decisions ORDER BY ticker")
            ]

    def balance(self):
        with self.store.connection() as db:
            return self.paper.repo.replay(db, self.paper.repo.current(db)["id"])

    def http(self):
        http = Mock(spec=["json", "received_at", "guard"])
        http.received_at = self.current
        http.json.return_value = chart("VST", self.session, opening="50", edges=("open",))
        return http

    def test_P15_snapshot_uses_paper_positions_and_never_real_holdings(self):
        Ledger(self.store).record("VST", "buy", "12", "100", "USD", "2026-11-03")
        with patch.object(Ledger, "positions", side_effect=AssertionError("real ledger")):
            self.paper.capture(self.session, now=self.current)
        decision = self.saved()[0]
        self.assertEqual(decision["held_quantity"], 0)
        self.assertEqual(decision["action"], "buy")
        self.assertNotIn("position_quantity", decision["inputs"])

    def test_P16_P17_single_preopen_snapshot_and_no_later_reclassification(self):
        queue = Mock()
        self.current = self.session.opens - 3600
        self.paper.schedule(queue, self.current)
        self.paper.schedule(queue, self.current)
        self.assertEqual(self.saved(), [])
        self.current = self.session.opens - 900
        self.paper.schedule(queue, self.current)
        before = self.saved()
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0]["scheduled_at"], self.current)
        self.store.remove("VST")
        self.paper.schedule(queue, self.current + 30)
        self.assertEqual(self.saved(), before)
        second = self.calendar.next_session(self.session.closes)
        self.current = second.opens - 840
        self.paper.schedule(queue, self.current)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT state FROM paper_session_state WHERE session_date=?", (second.day,)
                ).fetchone()[0],
                "missed",
            )
        self.assertEqual(self.saved(), before)

    def test_P16_calendar_holiday_dst_early_close_and_weekend(self):
        for day in (date(2026, 3, 9), date(2026, 11, 27)):
            session = self.calendar.session(day)
            self.current = session.opens - 900
            self.paper.schedule(Mock(), self.current)
            with self.store.connection() as db:
                row = db.execute(
                    "SELECT opens,closes FROM paper_batches WHERE session_date=?", (session.day,)
                ).fetchone()
                self.assertEqual(row, (session.opens, session.closes))
        self.current = self.session.opens + 4 * 86400  # Saturday
        self.paper.schedule(Mock(), self.current)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_batches").fetchone()[0], 2)

    def test_P17_all_100_symbols_get_a_decision_and_late_commit_rolls_back(self):
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for i in range(99):
            self.store.add("S" + alphabet[i // 26] + alphabet[i % 26], 2)
        with patch.object(
            self.paper,
            "inputs",
            side_effect=lambda db, ticker, session, now, run: inputs_for(session, ticker=ticker),
        ):
            self.paper.capture(self.session, now=self.current)
        self.assertEqual(len(self.saved()), 100)
        for page in range(1, 14):
            self.assertLess(
                len(self.paper.decisions(self.session.day, page).encode("utf-16-le")) // 2, 4096
            )
        second = self.calendar.next_session(self.session.closes)
        self.current = second.opens - 839
        with self.assertRaises(ValueError):
            self.paper.capture(second, now=second.opens - 900)
        self.assertEqual(len(self.saved()), 100)

    def test_P18_exact_open_first_allowed_at_300_and_expires_without_substitution(self):
        self.paper.capture(self.session, now=self.current)
        self.current = self.session.opens + 299
        http = self.http()
        self.paper.run_once(http, now=self.current)
        http.json.assert_not_called()
        self.current += 1
        http.received_at = self.current
        self.paper.run_once(http, now=self.current)
        self.assertEqual(http.json.call_count, 1)
        self.assertEqual(self.balance()["positions"]["VST"]["quantity"], 4)
        self.assertIn("filled", self.paper.decisions())

    def test_P18_missing_open_expires_without_fee_or_http_after_deadline(self):
        self.paper.capture(self.session, now=self.current)
        self.current = self.session.closes + 1800
        http = self.http()
        self.paper.run_once(http, now=self.current)
        http.json.assert_not_called()
        self.assertEqual(self.balance()["cash"], 2000)
        self.assertEqual(self.balance()["reserved"], 0)
        self.assertIn("expired", self.paper.decisions())

    def test_P19_storage_pause_inflight_blocks_commit_then_safe_expiry(self):
        self.paper.capture(self.session, now=self.current)
        self.current = self.session.opens + 300
        http = self.http()

        def response(*args, **kwargs):
            self.store.set_state("storage_paused", "low")
            return chart("VST", self.session, edges=("open",))

        http.json.side_effect = response
        with self.assertRaises(WorkStopped):
            self.paper.run_once(http, now=self.current)
        self.assertEqual(self.balance()["cash"], 2000)
        self.assertGreater(self.balance()["reserved"], 0)
        self.store.set_state("storage_paused", "")
        self.current = self.session.closes + 1800
        self.paper.run_once(http, now=self.current)
        self.assertEqual(self.balance()["reserved"], 0)

    def test_P19_disable_inflight_cancelled_order_stays_cancelled_on_resume(self):
        self.paper.capture(self.session, now=self.current)
        self.current = self.session.opens + 300
        http = self.http()

        def response(*args, **kwargs):
            self.paper.handle(["off"])
            self.paper.handle(["on"])
            return chart("VST", self.session, opening="50", edges=("open",))

        http.json.side_effect = response
        with self.assertRaises(WorkStopped):
            self.paper.run_once(http, now=self.current)
        self.assertEqual(self.balance()["cash"], 2000)
        self.assertEqual(self.balance()["reserved"], 0)
        self.assertIn("cancelled", self.paper.decisions())

    def test_P23_fx_paper_only_receipt_clock_hourly_quota_and_no_backdating(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM fx_observations")
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            return io.BytesIO(
                json.dumps(
                    {"base": "EUR", "date": self.session.day, "rates": {"USD": 1.1}}
                ).encode()
            )

        worker = FX(self.store, opener, clock=lambda: self.session.opens + 1)
        worker.refresh(self.current)
        worker.refresh(self.current + 60)
        self.assertEqual(len(calls), 1)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT retrieved_at FROM fx_cache").fetchone()[0],
                self.session.opens + 1,
            )
            self.assertIsNone(self.paper.repo.fx(db, self.session.opens))
        self.assertEqual(Ledger(self.store).positions("USD"), {})

    def test_P24_preparation_keeps_removed_paper_holding_and_has_no_alerts(self):
        self.paper.capture(self.session, now=self.current)
        self.current = self.session.opens + 300
        self.paper.run_once(self.http(), now=self.current)
        self.store.remove("VST")
        self.assertIn("VST", self.paper.coverage())
        self.insights.queue(
            "paper_prepare", origin="automatic", gate="paper", session=self.session.day
        )
        with (
            patch.object(self.insights, "feature", return_value=Mock()) as feature,
            patch.object(self.insights, "market_context", return_value=Mock()) as market,
        ):
            self.insights.process_job()
        self.assertEqual(feature.call_count, 3)
        for call in feature.call_args_list:
            self.assertTrue(call.kwargs["automatic"])
            self.assertEqual(call.kwargs["context"], "paper")
        for call in feature.return_value.refresh.call_args_list:
            self.assertEqual(call.args[0], ["VST"])
            self.assertFalse(call.kwargs["force"])
        market.return_value.refresh.assert_called_once()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.store.settings()["advisor_results_enabled"], "off")

    def test_P25_owner_authorization_grammar_cached_views_and_command_menu(self):
        telegram = Mock()
        bot = Bot(self.config, self.store, telegram, Mock())
        self.addCleanup(bot.scans.close)
        for command in ("/paper start", "/paper off", "/paper results", "/paper decisions"):
            for user, chat, kind in (
                (999, 123, "private"),
                (123, 999, "private"),
                (123, 123, "group"),
            ):
                bot.handle(
                    {
                        "message": {
                            "text": command,
                            "from": {"id": user},
                            "chat": {"id": chat, "type": kind},
                        }
                    }
                )
        telegram.send.assert_not_called()
        for args in (
            ["buy", "VST"],
            ["reset"],
            ["on", "extra"],
            ["decisions", "20261110"],
            ["decision", "unknown"],
        ):
            with self.assertRaises(ValueError):
                self.paper.handle(args)
        with patch("trabe.signals.SignalHTTP.json", side_effect=AssertionError("cached view HTTP")):
            for args in ([], ["status"], ["results"], ["decisions"], ["trades"]):
                self.paper.handle(args)
        self.assertEqual(sum(c[0] == "paper" for c in COMMANDS), 1)
        self.assertEqual(len(COMMANDS), 40)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM signal_jobs").fetchone()[0], 0)

    def test_P26_missing_marks_and_known_actions_do_not_fabricate_equity(self):
        self.paper.capture(self.session, now=self.current)
        self.current = self.session.opens + 300
        self.paper.run_once(self.http(), now=self.current)
        self.assertIn("Equity unavailable", self.paper.status(self.current))
        self.current = self.session.closes + 1800
        value = parse_endpoint(
            chart("VST", self.session, closing="55"),
            symbol="VST",
            session=self.session,
            edge="close",
            received_at=self.current,
        )
        with self.store.connection() as db:
            EndpointStore(self.store).save(value, db=db)
        self.paper.run_once(self.http(), now=self.current)
        self.assertIn("common close " + self.session.day, self.paper.status(self.current))
        second = self.calendar.next_session(self.session.closes)
        self.current = second.opens + 300
        value = parse_endpoint(
            chart("VST", second, edges=("open",)),
            symbol="VST",
            session=second,
            edge="open",
            received_at=self.current,
        )
        value["actions"] = [{"kind": "split", "at": second.opens}]
        with self.store.connection() as db:
            EndpointStore(self.store).save(value, db=db)
        self.paper.run_once(self.http(), now=self.current)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM paper_action_conflicts").fetchone()[0], 1
            )
            db.execute("DELETE FROM market_price_endpoints WHERE session_date=?", (second.day,))
        self.assertIn("corporate-action conflict", self.paper.status(self.current))
        self.assertIn("Equity unavailable", self.paper.status(self.current))

    def test_P27_packet_limit_rejects_entire_batch_and_retains_cash(self):
        values = inputs_for(self.session)
        values["news"][0]["snippet"] = "a" * 20000
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assertIsNone(
                self.paper.repo.capture_batch([values], self.session, now=self.current, db=db)
            )
            self.assertEqual(self.paper.repo.current(db)["status"], "paused")
        self.assertEqual(self.saved(), [])
        self.assertEqual(self.balance()["cash"], 2000)

    def test_P22_fx_migration_observes_old_cache_at_migration_time(self):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO fx_cache VALUES (?,?,?,?,?)",
                (
                    self.session.day,
                    "1.1",
                    self.session.day,
                    "Frankfurter/ECB",
                    self.current - 10000,
                ),
            )
            db.execute("DELETE FROM fx_observations")
            db.execute("PRAGMA user_version=10")
        with patch("trabe.store.time.time", return_value=self.session.opens + 1):
            Store(self.store.path)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT known_at FROM fx_observations").fetchone()[0],
                self.session.opens + 1,
            )
            self.assertIsNone(self.paper.repo.fx(db, self.session.opens))
