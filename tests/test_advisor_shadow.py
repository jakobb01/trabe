"""Prospective capture, same-window outcome reuse and matched comparisons."""

import copy
import sqlite3
import unittest
from unittest.mock import Mock, patch

import test_advisor as fixtures
from test_price_endpoints import chart

from trabe.advisor import CONSENSUS, MEAN_BIAS, Advisor
from trabe.advisor_comparison import ScoredForecast, disjoint
from trabe.advisor_results import AdvisorResults
from trabe.advisor_shadow import AdvisorShadow
from trabe.bot import Bot
from trabe.config import Config
from trabe.ledger import Ledger
from trabe.maintenance import Maintenance
from trabe.outbox import Outbox
from trabe.price_endpoints import parse_endpoint
from trabe.store import Store
from trabe.workers import Services


class ShadowTests(unittest.TestCase):
    def setUp(self):
        fixtures.AdvisorTests.setUp(self)
        self.first, *_, self.last = self.advisor._sessions(self.now)
        self.capture_at = self.first.opens - 550
        self.shadow = AdvisorShadow(self.store, self.advisor.calendar)
        self.signals.ok("news", "finnhub", "VST", self.capture_at - 120)

    def seed(self, *args, **kwargs):
        return fixtures.AdvisorTests.seed(self, *args, **kwargs)

    def enroll(self, when=None):
        with patch("trabe.store.time.time", return_value=when or self.capture_at - 3600):
            self.store.set_setting("advisor_shadow_enabled", "on")

    def test_capture_is_separate_idempotent_and_freezes_point_in_time(self):
        self.assertIsNone(self.shadow.capture(now=self.capture_at))
        self.enroll()
        with patch.object(Advisor, "comparison", side_effect=AssertionError("issuance only")):
            self.assertIn("Preview only", self.advisor.preview("VST", self.capture_at))
            self.assertIn("Frozen 0", self.advisor.handle(["compare"]))
        checkpoint = self.shadow.capture(now=self.capture_at)
        self.assertEqual(checkpoint["captured"], ["VST"])
        self.assertIsNone(self.advisor.get("VST"))
        manual = self.advisor.issue("VST", self.capture_at + 4)
        restarted = Store(self.store.path)
        self.assertIsNone(AdvisorShadow(restarted).capture(now=self.capture_at + 30))
        with patch.object(Advisor, "comparison", side_effect=AssertionError("retry")):
            self.assertEqual(Advisor(restarted).issue("VST", self.capture_at + 30), manual)
        with restarted.connection() as db:
            [(packet, result)] = AdvisorResults(restarted, shadow=True).saved(db, "VST")
            self.assertIsNone(result)
            self.assertEqual(packet["as_of"], self.capture_at)
            self.assertEqual(packet["forecast"]["direction"], "bullish")
            self.assertEqual(packet["comparison"], manual["comparison"])
            self.assertEqual(packet["sessions"], manual["sessions"])
            self.assertNotEqual(packet["id"], manual["id"])
            self.assertLessEqual(packet["inputs"]["price"]["received_at"], self.capture_at)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE shadow_company_advice SET payload='{}'")
        self.assertEqual(self.news.http.calls, [])

    def test_window_pause_retention_rotation_and_missed_checkpoint(self):
        self.enroll()
        for ticker in ("AMD", "GOOGL", "IBM", "QQQ", "TSLA"):
            self.store.add(ticker, 1)
        Ledger(self.store).record("NVDA", "buy", "1", "100", "USD", "2026-11-02")
        self.assertIsNone(self.shadow.capture(now=self.first.opens - 601))
        self.store.pause_automation(self.first.opens)
        self.assertIsNone(self.shadow.capture(now=self.capture_at))
        self.store.pause_automation(0)
        self.assertIsNone(self.shadow.capture(now=self.first.opens - 60))
        self.assertIn("coverage is unknown", self.shadow.status(now=self.first.opens))
        self.assertIn("retention: 1", self.shadow.status(now=self.first.opens))
        second, third, *_ = self.advisor._sessions(self.first.closes)
        first_capture = self.shadow.capture(now=second.opens - 600)
        self.assertEqual(first_capture["covered"], 7)
        self.assertEqual(first_capture["selected"], ["AMD", "GOOGL", "IBM", "NVDA", "QQQ"])
        second_capture = self.shadow.capture(now=third.opens - 600)
        self.assertEqual(second_capture["selected"], ["TSLA", "VST", "AMD", "GOOGL", "IBM"])
        self.assertIn("10/10 selected companies captured", self.shadow.status(now=third.opens))
        self.assertIn("abstained 10", self.advisor.handle(["compare"]))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(AdvisorResults(self.store, shadow=True).ensure_pending(now=third.opens), 0)
        Maintenance(self.store).expire(fixtures.stamp("2027-03-15T12:00:00+00:00"))
        self.assertIn("Retained checkpoints: 0", self.shadow.status(now=third.opens))

    def test_manual_and_shadow_share_endpoints_and_two_requests_per_pass(self):
        self.enroll()
        self.store.set_setting("advisor_results_enabled", "on")
        self.store.add("IBM", 1)  # A shadow abstention incurs no request.
        self.shadow.capture(now=self.capture_at)
        self.advisor.issue("VST", self.capture_at)
        config = Config("100:" + "a" * 32, 123, "", self.store.path)
        services = Services(self.store, config, Mock(spec=[]), Outbox(self.store), Mock())
        self.addCleanup(services.close)
        http = Mock(spec=["json", "received_at"])
        for edge, session in (("open", self.first), ("close", self.last)):
            now = session.opens + 300 if edge == "open" else session.closes + 1800
            services.advisor_results.clock = services.shadow_results.clock = lambda: now
            http.received_at = now
            http.json.side_effect = lambda url, **kw: chart(
                "SPY" if "/SPY?" in url else "VST", session, edges=(edge,)
            )
            with patch("trabe.workers.SignalHTTP", return_value=http):
                services.collect_company_outcomes(now)
            self.assertEqual(http.json.call_count, 2)
            http.json.reset_mock()
        with self.store.connection() as db:
            [(shadow, result)] = services.shadow_results.saved(db, "VST")
            self.assertEqual(result["status"], "scored")
            self.assertEqual(result["sessions"], shadow["sessions"])
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM shadow_advisor_outcome_inputs").fetchone()[0], 4
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM market_price_endpoints").fetchone()[0], 4
            )

    def test_owner_grammar_and_legacy_retry_stay_read_only(self):
        config = Config("100:" + "a" * 32, 123, "", self.store.path)
        telegram = Mock()
        bot = Bot(config, self.store, telegram, Mock())
        self.addCleanup(bot.scans.close)
        for command in ("/advisor compare", "/advisor shadow on"):
            bot.handle(
                {
                    "message": {
                        "text": command,
                        "from": {"id": 999},
                        "chat": {"id": 123, "type": "private"},
                    }
                }
            )
        telegram.send.assert_not_called()
        self.assertEqual(self.store.settings()["advisor_shadow_enabled"], "off")
        self.assertIn("/advisor compare", bot.command("advisor", []))
        self.assertIn("/advisor compare", bot.command("advisor", ["results"]))
        self.assertIn("sampling: on", bot.command("advisor", ["shadow", "on"]))
        with self.store.connection() as db:
            inputs = self.advisor._inputs(db, "VST", self.capture_at, self.first)
            legacy = self.advisor.freeze(
                "VST", self.capture_at, self.advisor._sessions(self.capture_at), inputs
            )
            del legacy["comparison"]
            self.advisor.save(db, legacy)
        with patch.object(Advisor, "comparison", side_effect=AssertionError("legacy")):
            self.assertEqual(self.advisor.issue("VST", self.capture_at), legacy)
            self.assertIn("1 legacy", bot.command("advisor", ["compare", "VST"]))

    def test_candidate_boundaries_opposing_votes_and_group_conflict(self):
        with self.store.connection() as db:
            inputs = self.advisor._inputs(db, "VST", self.capture_at, self.first)
        for biases, conflict, missing, expected in (
            (["0.2", "0"], False, False, ("mixed", "bullish", "bullish")),
            (["-0.2", "0"], False, False, ("mixed", "bearish", "bearish")),
            (["0.8", "-0.2"], False, False, ("mixed", "mixed", "bullish")),
            (["0.8", "0.8"], True, False, ("mixed", "mixed", "mixed")),
            (["0.8", "0.8"], False, True, ("insufficient", "insufficient", "insufficient")),
        ):
            with self.subTest(biases=biases, conflict=conflict, missing=missing):
                snapshot = copy.deepcopy(inputs)
                for story, bias in zip(snapshot["news"], biases, strict=True):
                    story["analysis"]["bias"] = bias
                snapshot["news"][0]["analysis"]["mixed"] = conflict
                if missing:
                    snapshot["price"] = None
                forecast = self.advisor.infer(snapshot, self.capture_at, self.last)
                comparison = self.advisor.comparison(snapshot, self.capture_at, forecast)
                self.assertEqual(
                    (
                        forecast["direction"],
                        comparison["directions"][CONSENSUS],
                        comparison["directions"][MEAN_BIAS],
                    ),
                    expected,
                )

    def test_disjoint_chooses_earliest_end_and_keeps_multiple_companies(self):
        one, later, disjoint_window = [
            tuple(session.day for session in self.advisor._sessions(now))
            for now in (self.now, self.first.closes, self.last.closes)
        ]
        scored = [
            ScoredForecast(later, "VST", {}, "mixed"),
            ScoredForecast(one, "VST", {}, "mixed"),
            ScoredForecast(disjoint_window, "VST", {}, "mixed"),
            ScoredForecast(one, "NVDA", {}, "mixed"),
        ]
        windows, rows = disjoint(scored)
        self.assertEqual(windows, {one, disjoint_window})
        self.assertEqual(rows, scored[1:])

    def test_aggregate_denominators_paired_outcomes_and_telegram_limit(self):
        horizon = self.advisor._sessions(self.capture_at)
        settled = self.last.closes + 1800
        with self.store.connection() as db:
            inputs = self.advisor._inputs(db, "VST", self.capture_at, self.first)
            inputs["news"][0]["analysis"]["bias"] = "0.2"
            inputs["news"][1]["analysis"]["bias"] = "0"
            for shadow in (False, True):
                results = AdvisorResults(self.store, shadow=shadow)
                for ticker, close in (
                    ("AMD", "102"),
                    ("AMZN", "100"),
                    ("GOOGL", "98"),
                    ("IBM", "missing"),
                    ("NVDA", None),
                    ("VST", None),
                ):
                    evidence = inputs
                    if ticker == "VST":
                        evidence = inputs | {
                            "price": None,
                            "news": [],
                            "news_sources": [{"succeeded_at": None, "error": "failed"}],
                        }
                    packet = self.advisor.freeze(ticker, self.capture_at, horizon, evidence)
                    self.advisor.save(db, packet, shadow=shadow)
                    if close is None:
                        continue
                    observations = {}
                    if close != "missing":
                        for edge, session in (("open", self.first), ("close", self.last)):
                            observations["company/" + edge] = parse_endpoint(
                                chart(ticker, session, opening="100", closing=close, edges=(edge,)),
                                symbol=ticker,
                                session=session,
                                edge=edge,
                                received_at=settled,
                            )
                    results._save(db, results.assess(packet, observations, evaluated_at=settled))
        text = self.advisor.handle(["compare"])
        self.assertEqual(text.count("Frozen 6 • pending 1 • abstained 1 • unscorable 1"), 2)
        self.assertEqual(text.count("directional 5/6 frozen, 3/3 scored"), 4)
        self.assertEqual(text.count("Matched 1/3 scored, 1/3 disjoint"), 6)
        self.assertEqual(text.count("3 scored disagreements: 1 wins, 1 losses, 1 both missed"), 4)
        self.assertEqual(
            text.count("Need two distinct recent company stories with extracts. [1]"), 2
        )
        self.assertIn("2026-11-10 to 2026-11-16", text)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
