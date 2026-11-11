"""Prospective endpoint capture, frozen assessments, migration and cached controls."""

import copy
import sqlite3
import unittest
from datetime import date
from unittest.mock import Mock, patch

import test_advisor as advisor_fixtures
from test_advisor import stamp
from test_price_endpoints import chart

from trabe.advisor import Advisor
from trabe.advisor_results import AdvisorResults
from trabe.calendar import USCalendar
from trabe.ledger import Ledger
from trabe.maintenance import Maintenance
from trabe.outlook import digest
from trabe.price_endpoints import EndpointStore, parse_endpoint
from trabe.signals import WorkStopped
from trabe.store import SCHEMA_VERSION, Store


class ResultsTests(unittest.TestCase):
    def setUp(self):
        # Share realistic source fixtures without inheriting/re-running the advisor suite.
        advisor_fixtures.AdvisorTests.setUp(self)
        self.store.set_setting("advisor_results_enabled", "on")
        self.packet = self.advisor.issue("VST", self.now)
        self.first = self.advisor.calendar.session(date.fromisoformat(self.packet["sessions"][0]))
        self.last = self.advisor.calendar.session(date.fromisoformat(self.packet["sessions"][-1]))
        self.current = self.first.opens + 300
        self.results = AdvisorResults(self.store, clock=lambda: self.current)

    def seed(self, *args, **kwargs):
        return advisor_fixtures.AdvisorTests.seed(self, *args, **kwargs)

    def observations(self, end="102", spy="202"):
        result = {}
        for role, symbol in (("company", "VST"), ("benchmark", "SPY")):
            for edge, session in (("open", self.first), ("close", self.last)):
                raw = chart(
                    symbol,
                    session,
                    opening="100" if role == "company" else "200",
                    closing=end if role == "company" else spy,
                )
                result[role + "/" + edge] = parse_endpoint(
                    raw,
                    symbol=symbol,
                    session=session,
                    edge=edge,
                    received_at=session.closes + 1800,
                )
        return result

    def test_A14_A15_decimal_boundaries_and_same_window_benchmark(self):
        for end, category in (("101", "bullish"), ("99", "bearish"), ("100.999", "mixed")):
            result = self.results.assess(
                self.packet, self.observations(end), evaluated_at=self.last.closes + 1800
            )
            self.assertEqual(result["realized"], category)
        result = self.results.assess(
            self.packet, self.observations(), evaluated_at=self.last.closes + 1800
        )
        self.assertEqual(result["company_return"], "2.00")
        self.assertEqual(result["spy_return"], "1.00")
        self.assertEqual(result["excess_percentage_points"], "1.00")
        self.assertTrue(result["matched"])

    def test_A16_abstention_missing_company_and_missing_benchmark(self):
        observations = self.observations()
        packet = copy.deepcopy(self.packet)
        packet["forecast"]["direction"] = "insufficient"
        self.assertEqual(
            self.results.assess(packet, observations, evaluated_at=self.last.closes + 1800)[
                "status"
            ],
            "abstained",
        )
        observations.pop("benchmark/open")
        result = self.results.assess(
            self.packet, observations, evaluated_at=self.last.closes + 1800
        )
        self.assertEqual(result["status"], "scored")
        self.assertIsNone(result["spy_return"])
        observations.pop("company/close")
        result = self.results.assess(
            self.packet, observations, evaluated_at=self.last.closes + 1800
        )
        self.assertEqual(result["status"], "unscorable")
        self.assertIsNone(result["matched"])

    def test_A07_A08_A17_A24_capture_open_then_later_close_and_restart(self):
        self.store.add("GOOGL", 1)
        self.advisor.issue("GOOGL", self.now)
        self.store.remove("VST")
        self.results.ensure_pending(now=self.current)
        http = Mock(spec=["json", "received_at"])
        http.received_at = self.current
        http.json.side_effect = lambda url, **kw: chart(
            "SPY" if "/SPY?" in url else "VST" if "/VST?" in url else "GOOGL",
            self.first,
            edges=("open",),
        )
        self.results.run_once(http, now=self.current)
        self.results.run_once(http, now=self.current)
        self.assertEqual(http.json.call_count, 3)  # shared SPY, two companies
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM advisor_evaluations").fetchone()[0], 0
            )
        self.current = self.last.closes + 1800
        http.received_at = self.current
        http.json.side_effect = lambda url, **kw: chart(
            "SPY" if "/SPY?" in url else "VST" if "/VST?" in url else "GOOGL",
            self.last,
            edges=("close",),
        )
        restarted = AdvisorResults(Store(self.store.path), clock=lambda: self.current)
        restarted.run_once(http, now=self.current)
        restarted.run_once(http, now=self.current)
        self.assertIn("scored", restarted.results(advice_id=self.packet["id"]))
        self.assertEqual(http.json.call_count, 6)
        self.assertEqual(self.advisor.get("VST"), self.packet)
        restarted.run_once(http, now=self.current)
        self.assertEqual(http.json.call_count, 6)

    def test_A08_spy_as_company_has_four_roles_two_physical_endpoints(self):
        self.store.add("SPY", 1)
        packet = self.advisor.issue("SPY", self.now)
        self.results.ensure_pending(now=self.current)
        value = parse_endpoint(
            chart("SPY", self.first, edges=("open",)),
            symbol="SPY",
            session=self.first,
            edge="open",
            received_at=self.current,
        )
        with self.store.connection() as db:
            EndpointStore(self.store).save(value, db=db)
        self.results._advance(self.current)
        with self.store.connection() as db:
            ids = db.execute(
                "SELECT endpoint_id FROM advisor_outcome_inputs WHERE advice_id=? AND edge='open'",
                (packet["id"],),
            ).fetchall()
            self.assertEqual(len(ids), 2)
            self.assertEqual(ids[0], ids[1])

    def test_A09_A18_unsupported_and_action_conflicts(self):
        for field in ("version", "flat_band_percent", "benchmark"):
            packet = copy.deepcopy(self.packet)
            packet["assessment"][field] = "unknown"
            self.assertEqual(
                self.results.assess(
                    packet, self.observations(), evaluated_at=self.last.closes + 1800
                )["status"],
                "unsupported",
            )
        obs = self.observations()
        obs["company/close"]["actions"] = [{"kind": "split", "at": self.last.opens}]
        result = self.results.assess(self.packet, obs, evaluated_at=self.last.closes + 1800)
        self.assertEqual(result["status"], "unscorable")
        self.assertIn("unverified", result["quality"])

    def test_A10_inflight_off_pause_or_storage_never_persists(self):
        for reason in ("off", "pause", "storage"):
            self.store.set_setting("advisor_results_enabled", "on")
            self.store.set_state("storage_paused", "")
            self.store.pause_automation(0)
            with self.store.connection() as db:
                db.execute("DELETE FROM market_endpoint_attempts")
            http = Mock(spec=["json", "received_at", "guard"])
            http.received_at = self.current

            def response(*args, **kwargs):
                if reason == "off":
                    self.store.set_setting("advisor_results_enabled", "off")
                elif reason == "pause":
                    self.store.pause_automation(self.current + 3600)
                else:
                    self.store.set_state("storage_paused", "low")
                return chart("VST", self.first, edges=("open",))

            http.json.side_effect = response
            with self.assertRaises(WorkStopped):
                self.results.run_once(http, now=self.current)
            with self.store.connection() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM market_price_endpoints").fetchone()[0], 0
                )
            self.assertIn("pending", self.results.results(advice_id=self.packet["id"]))

    def test_A11_no_early_expired_or_repeated_requests(self):
        http = Mock(spec=["json", "received_at"])
        self.current -= 1
        self.assertFalse(self.results.run_once(http, now=self.current))
        http.json.assert_not_called()
        self.current = self.last.closes + 7 * 86400
        self.results.run_once(http, now=self.current)
        http.json.assert_not_called()
        self.assertIn("unscorable", self.results.results(advice_id=self.packet["id"]))

    def test_A13_evaluations_cannot_update_replace_or_hide_corruption(self):
        value = self.results.assess(
            self.packet, self.observations(), evaluated_at=self.last.closes + 1800
        )
        with self.store.connection() as db:
            self.results._save(db, value)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE advisor_evaluations SET payload='{}'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT OR REPLACE INTO advisor_evaluations SELECT * FROM advisor_evaluations"
                )
            db.execute("DROP TRIGGER immutable_advisor_result")
            db.execute("UPDATE advisor_evaluations SET sha256='corrupt'")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.results.results()

    def test_A19_A20_migration_and_expiry_preserve_real_money(self):
        Ledger(self.store).record("VST", "buy", "2", "100", "USD", "2026-11-03")
        self.results.ensure_pending(now=self.current)
        with self.store.connection() as db:
            old = db.execute("SELECT payload,sha256 FROM company_advice").fetchall()
            db.execute("PRAGMA user_version=9")
        Store(self.store.path)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT payload,sha256 FROM company_advice").fetchall(), old
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        Maintenance(self.store).expire(stamp("2027-03-01T12:00:00+00:00"))
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM advisor_outcome_inputs").fetchone()[0], 0
            )
        self.assertEqual(Ledger(self.store).positions("USD")["VST"][0], 2)

    def test_A21_A22_cached_grammar_counts_and_original_record(self):
        advisor = Advisor(self.store)
        before = digest(self.packet)
        for args in (
            ["result", "bad"],
            ["record", "a" * 16],
            ["results", "VST", "oops"],
            ["track"],
            ["results", "VST", "extra"],
        ):
            with self.assertRaises(ValueError):
                advisor.handle(args)
        with patch("trabe.advisor.Advisor.issue", side_effect=AssertionError("must be cached")):
            text = advisor.handle(["results"])
            self.assertIn("n/a (0 eligible)", text)
            self.assertIn("pending 1", text)
            self.assertLess(len(text.encode("utf-16-le")) // 2, 4096)
            self.assertIn(self.packet["id"], advisor.handle(["record", self.packet["id"]]))
            self.assertIn("pending", advisor.handle(["result", self.packet["id"]]))
        self.assertEqual(digest(self.advisor.get("VST")), before)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM signal_jobs").fetchone()[0], 0)

    def test_A24_expired_calendar_finalizes_unsupported_without_spinning(self):
        calendar = USCalendar()
        calendar.data["years"] = {}
        result = AdvisorResults(self.store, calendar, clock=lambda: self.current)
        result.run_once(Mock(), now=self.current)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT status FROM advisor_evaluations").fetchone()[0], "unsupported"
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM market_endpoint_attempts").fetchone()[0], 0
            )

    def test_A11_failed_oldest_does_not_starve_later_due_work(self):
        from trabe.api import ApiError

        self.store.add("AAA", 1)
        self.advisor.issue("AAA", self.now)
        self.results.ensure_pending(now=self.current)
        http = Mock(spec=["json", "received_at"])
        http.received_at = self.current

        def response(url, **kwargs):
            if "/SPY?" in url:
                raise ApiError("unavailable")
            return chart("AAA" if "/AAA?" in url else "VST", self.first, edges=("open",))

        http.json.side_effect = response
        self.results.run_once(http, now=self.current)
        self.results.run_once(http, now=self.current)
        self.assertIsNotNone(EndpointStore(self.store).get("VST", self.first.day, "open"))
        self.assertEqual(http.json.call_count, 3)

    def test_A20_shared_pending_endpoints_survive_parent_expiry(self):
        self.results.ensure_pending(now=self.current)
        value = self.observations()["benchmark/open"]
        with self.store.connection() as db:
            EndpointStore(self.store).save(value, db=db)
            EndpointStore(self.store).prune(self.current, db)
        self.assertIsNotNone(EndpointStore(self.store).get("SPY", self.first.day, "open"))

    def test_A22_eligible_baselines_exclude_abstentions_and_pending(self):
        self.store.add("GOOGL", 1)
        abstention = self.advisor.issue("GOOGL", self.now)
        self.store.add("IBM", 1)
        self.advisor.issue("IBM", self.now)
        with self.store.connection() as db:
            self.results._save(
                db,
                self.results.assess(
                    self.packet, self.observations(), evaluated_at=self.last.closes + 1800
                ),
            )
            self.results._save(
                db, self.results.assess(abstention, {}, evaluated_at=self.last.closes + 1800)
            )
        text = self.results.results()
        self.assertIn("Matched 1/1", text)
        self.assertIn("always bullish 1/1; always mixed 0/1", text)
        self.assertIn("abstained 1", text)
        self.assertIn("pending 1", text)
        self.assertIn("1 paired records", text)

    def test_A21_owner_checks_protect_results_and_tracking(self):
        from trabe.bot import Bot
        from trabe.config import Config

        telegram = Mock()
        bot = Bot(Config("100:" + "a" * 32, 123, "", self.store.path), self.store, telegram, Mock())
        self.addCleanup(bot.scans.close)
        for command in (
            "/advisor results",
            "/advisor track off",
            "/advisor result " + self.packet["id"],
        ):
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
        self.assertEqual(self.store.settings()["advisor_results_enabled"], "on")
        telegram.send.assert_not_called()
