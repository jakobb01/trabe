"""Pre-open expectations: known inputs, atomic delivery and honest later grading."""

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from trabe.bot import Bot
from trabe.calendar import NY, USCalendar
from trabe.config import Config
from trabe.insights import Insights
from trabe.ledger import Ledger
from trabe.maintenance import Maintenance
from trabe.outbox import Outbox
from trabe.outlook import OutlookService, OutlookStore, digest, outcome_chart
from trabe.signals import SignalStore
from trabe.store import Store


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


class OutlookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "agent.db")
        self.now = stamp("2026-11-09T14:00:00+00:00")
        self.calendar = USCalendar()
        self.session = self.calendar.at(self.now)
        self.service = OutlookService(self.store)
        self.signals = SignalStore(self.store)
        self.box = Outbox(self.store)
        self.config = Config("100:" + "a" * 32, 123, "", self.store.path)
        self.insights = Insights(self.config, self.store, self.box)
        self.store.set_setting("outlook_enabled", "on")
        self.seed()

    def seed(self, moves=("1", "1"), *, now=None, day="2026-11-09", vix="24"):
        now = self.now if now is None else now
        previous = self.calendar.previous(date.fromisoformat(day))
        for symbol, move in zip(("SPY", "QQQ"), moves):
            self.signals.put(
                "market-context",
                symbol,
                symbol,
                "yahoo-chart",
                {
                    "symbol": symbol,
                    "currency": "USD",
                    "received_at": now,
                    "regular_price": "100",
                    "regular_time": previous.closes,
                    "regular_day": previous.day,
                    "previous_close": None,
                    "previous_close_day": None,
                    "premarket_session": day,
                    "premarket_price": str(100 + float(move)),
                    "premarket_time": now - 60,
                    "source_url": "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol,
                },
                now=now,
            )
        self.signals.put(
            "market-context",
            "^VIX",
            "^VIX",
            "yahoo-chart",
            {
                "symbol": "^VIX",
                "currency": "points",
                "received_at": now,
                "regular_price": vix,
                "regular_time": previous.closes + 901,
                "regular_day": previous.day,
                "delay_minutes": 15,
            },
            now=now,
        )
        self.signals.put(
            "market-news",
            "latest",
            "",
            "finnhub",
            {
                "source": "finnhub",
                "received_at": now,
                "items": [
                    {
                        "headline": "US stock futures rise ahead of inflation report",
                        "snippet": "Investors await consumer prices data.",
                        "published_at": now - 3600,
                        "url": "https://example.com/macro",
                        "provider": "finnhub",
                        "publisher": "Fixture",
                        "input_quality": "snippet",
                    }
                ],
            },
            now=now,
        )

    def outcome(self, symbol, *, open_price=101, close_price=102):
        return {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {
                            "symbol": symbol,
                            "currency": "USD",
                            "instrumentType": "ETF",
                            "dataGranularity": "1m",
                            "exchangeTimezoneName": "America/New_York",
                        },
                        "timestamp": [self.session.opens, self.session.closes - 60],
                        "indicators": {
                            "quote": [{"open": [open_price, 102], "close": [101.1, close_price]}]
                        },
                    }
                ],
            }
        }

    def http(self, *, closes=(102, 102), fail=False):
        outer = self

        class HTTP:
            def __init__(self):
                self.calls = []

            def json(self, url, provider):
                self.calls.append(url)
                if fail:
                    raise ValueError("private provider detail")
                symbol = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                return outer.outcome(symbol, close_price=closes[("SPY", "QQQ").index(symbol)])

        return HTTP()

    def test_direction_and_volatility_are_distinct_and_no_probability_is_invented(self):
        for index, (moves, expected) in enumerate(
            (
                (("1", "1"), "bullish"),
                (("-1", "-1"), "bearish"),
                (("1", "-1"), "mixed"),
                (("0.1", "0.1"), "mixed"),
            )
        ):
            now = self.now + index * 61
            self.seed(moves, now=now, vix="35")
            packet = self.service.issue(now=now)
            self.assertEqual(packet["forecast"]["opening"], expected)
            self.assertEqual(packet["forecast"]["session"], expected)
            self.assertIn("inflation data", packet["forecast"]["text"])
            self.assertIn("VIX 35.00 (prior session)", packet["forecast"]["text"])
            self.assertIn("elevated swing risk", packet["forecast"]["text"])
            self.assertNotIn("confidence", packet["forecast"])
            self.assertNotIn("probability", packet["forecast"])

    def test_stale_wrong_session_or_future_premarket_never_becomes_a_prediction(self):
        for index, update in enumerate(
            (
                {"premarket_time": self.now - 1800},
                {"premarket_session": "2026-11-06"},
                {"premarket_time": self.now + 900},
                {"premarket_price": "NaN"},
            )
        ):
            now = self.now + index * 61
            self.seed(now=now)
            value = self.signals.get("market-context", "SPY")["payload"] | update
            self.signals.put("market-context", "SPY", "SPY", "yahoo-chart", value, now=now)
            packet = self.service.issue(now=now)
            self.assertEqual(packet["forecast"]["opening"], "insufficient")
            self.assertEqual(packet["forecast"]["session"], "insufficient")
            self.assertTrue(packet["gaps"])

    def test_missing_context_abstains_for_session_without_hiding_valid_opening(self):
        self.signals.delete("market-context", "^VIX")
        self.signals.delete("market-news", "latest")
        packet = self.service.issue(now=self.now)
        self.assertEqual(packet["forecast"]["opening"], "bullish")
        self.assertEqual(packet["forecast"]["session"], "insufficient")
        self.assertIn("session evidence insufficient", packet["forecast"]["text"])
        self.assertIn("Fresh broad-market news unavailable", packet["gaps"])

    def test_future_receipts_publications_and_company_evidence_are_excluded(self):
        value = self.signals.get("market-context", "SPY")["payload"]
        value["received_at"] = self.now + 1
        self.signals.put("market-context", "SPY", "SPY", "yahoo-chart", value, now=self.now)
        news = self.signals.get("market-news", "latest")["payload"]
        news["items"][0]["published_at"] = self.now + 1
        self.signals.put("market-news", "latest", "", "finnhub", news, now=self.now)
        self.store.add("VST", 2)
        self.store.set_state(
            "news_view_VST",
            json.dumps({"as_of": self.now, "evidence": [{"known_at": self.now + 1}]}),
        )
        packet = self.service.issue(now=self.now)
        self.assertIsNone(packet["inputs"]["market"]["SPY"])
        self.assertEqual(packet["inputs"]["market_news"]["value"]["items"], [])
        self.assertEqual(packet["inputs"]["company_news"], [])
        self.assertEqual(packet["forecast"]["opening"], "insufficient")

    def test_frozen_record_survives_refresh_restart_and_rejects_replacement(self):
        packet = self.service.issue(now=self.now)
        self.assertEqual(packet["inputs_sha256"], digest(packet["inputs"]))
        self.seed(("-2", "-2"), now=self.now + 61)
        saved = OutlookStore(Store(self.store.path)).get()
        self.assertEqual(saved, packet)
        with self.store.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE market_outlooks SET payload='{}'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT OR REPLACE INTO market_outlooks SELECT * FROM market_outlooks")

    def test_issue_claim_and_delivery_are_atomic_and_restart_is_idempotent(self):
        with patch("trabe.outbox.Outbox.send", side_effect=ValueError("queue full")):
            with self.assertRaises(ValueError):
                self.service.issue("scheduled", now=self.now, owner_id=123)
        self.assertIsNone(self.service.repo.get())
        packet = self.service.issue("scheduled", now=self.now, owner_id=123)
        count = self.box.pending()
        restarted = OutlookService(Store(self.store.path))
        self.assertEqual(restarted.issue("scheduled", now=self.now + 60, owner_id=123), packet)
        self.assertEqual(self.box.pending(), count)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_outlooks").fetchone()[0], 1)

    def test_scheduler_uses_exchange_open_across_dst_holidays_travel_and_missed_window(self):
        for day in ("2026-03-09", "2026-10-26", "2026-11-27"):
            session = self.calendar.session(date.fromisoformat(day))
            self.store.set_setting("timezone", "Pacific/Auckland")
            with patch.object(self.insights, "queue", return_value=1) as queue:
                self.service.schedule(self.insights, session.opens - 1860)
                self.assertEqual(queue.call_args.kwargs["deadline"], session.opens - 1800)
                self.assertEqual(queue.call_args.kwargs["session"], day)
            self.service.schedule(self.insights, session.opens - 1800)
            count = self.box.pending()
            self.store.set_setting("timezone", "Europe/Ljubljana")
            self.service.schedule(self.insights, session.opens - 1740)
            self.assertEqual(self.box.pending(), count)
        count = self.box.pending()
        self.service.schedule(self.insights, stamp("2026-11-26T14:00:00+00:00"))
        self.service.schedule(self.insights, self.now + 301)
        self.assertEqual(self.box.pending(), count)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT status FROM outlook_runs WHERE session_date='2026-11-09'"
                ).fetchone()[0],
                "missed",
            )
        self.service.schedule(self.insights, stamp("2029-01-02T14:00:00+00:00"))
        self.assertIn("calendar expired", self.store.state("outlook_error"))

    def test_pause_disable_and_storage_health_block_capture_and_drop_proactive_message(self):
        self.store.pause_automation(self.now + 300)
        self.assertIsNone(self.service.issue("scheduled", now=self.now, owner_id=123))
        self.store.set_state("pause_until", "0")
        self.store.set_state("storage_paused", "full")
        with self.assertRaisesRegex(ValueError, "storage"):
            self.service.issue(now=self.now)
        self.store.set_state("storage_paused", "")
        self.service.issue("scheduled", now=self.now, owner_id=123)
        self.insights.handle("outlook", ["off"])
        self.assertEqual(self.box.pending(), 0)
        self.box.send(123, "disabled outlook", kind="outlook")
        self.box.send(123, "manual reply")

        class Sender:
            texts = []

            def send(self, chat_id, text, **kw):
                self.texts.append(text)

        sender = Sender()
        self.box.flush(sender)
        self.assertEqual(sender.texts, ["manual reply"])

    def test_outcome_waits_for_close_and_requires_exact_minute_endpoints_and_identity(self):
        now = self.session.closes + 1800
        value = self.outcome("SPY")
        self.assertEqual(outcome_chart(value, "SPY", self.session, now)["open"], "101")
        with self.assertRaises(ValueError):
            outcome_chart(value, "SPY", self.session, now - 1)
        with self.assertRaises(ValueError):
            outcome_chart(value, "QQQ", self.session, now)
        for change in (-86400, 60):
            bad = copy.deepcopy(value)
            bad["chart"]["result"][0]["timestamp"][0] += change
            with self.assertRaises(KeyError):
                outcome_chart(bad, "SPY", self.session, now)

    def test_assessment_separates_opening_gap_from_regular_session_and_does_not_rewrite(self):
        packet = self.service.issue("scheduled", now=self.now)
        http = self.http(closes=(100, 100))
        self.service.evaluate(http, now=self.session.closes + 1799)
        self.assertEqual(http.calls, [])
        self.service.evaluate(http, now=self.session.closes + 1800)
        with self.store.connection() as db:
            row = db.execute("SELECT payload,sha256 FROM outlook_evaluations").fetchone()
        assessed = self.service.repo.unpack(row)
        self.assertTrue(assessed["assessment"]["opening"]["matched"])
        self.assertFalse(assessed["assessment"]["session"]["matched"])
        self.assertEqual(assessed["assessment"]["session"]["realized"], "bearish")
        self.assertEqual(self.service.repo.get(), packet)
        self.service.evaluate(http, now=self.session.closes + 3600)
        self.assertEqual(len(http.calls), 2)
        results = self.service.results()
        self.assertIn("Opening: 1/1", results)
        self.assertIn("Session: 0/1", results)
        self.assertIn("always bullish", results)
        with self.store.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE outlook_evaluations SET payload='{}'")

    def test_missing_outcomes_retry_with_cooldown_then_remain_unscorable(self):
        self.service.issue("scheduled", now=self.now)
        http = self.http(fail=True)
        now = self.session.closes + 1800
        self.service.evaluate(http, now=now)
        self.service.evaluate(http, now=now + 60)
        self.assertEqual(len(http.calls), 2)
        self.assertNotIn("private", self.service.results())
        self.service.evaluate(http, now=self.session.closes + 8 * 86400)
        self.assertEqual(len(http.calls), 2)
        with self.store.connection() as db:
            value = json.loads(db.execute("SELECT payload FROM outlook_evaluations").fetchone()[0])
        self.assertIsNone(value["assessment"]["opening"]["matched"])
        self.assertIn("unscorable", value["gap"])

    def test_manual_job_only_refreshes_three_market_series_and_cached_views_do_no_io(self):
        from test_market_context import chart
        from test_signals import FixtureHTTP

        class HTTP(FixtureHTTP):
            calls = []

            def json(self, url, provider=None):
                symbol = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                self.calls.append(symbol)
                return chart(symbol)

        controls = Insights(self.config, self.store, self.box, http_factory=HTTP)
        with patch("time.time", return_value=self.now):
            controls.handle("outlook", [])
            controls.process_job()
        self.assertCountEqual(HTTP.calls, ["SPY", "QQQ", "^VIX"])
        count = len(HTTP.calls)
        self.assertIn("Outlook (rules)", controls.handle("outlook", ["cached"]))
        self.assertIn("Outlook (rules)", controls.render_briefing([], self.now))
        self.assertEqual(len(HTTP.calls), count)
        self.assertEqual(self.box.pending(), 1)

    def test_automatic_warmup_deadline_cancellation_never_becomes_manual_delivery(self):
        from test_signals import FixtureHTTP

        controls = Insights(self.config, self.store, self.box, http_factory=FixtureHTTP)
        controls.queue(
            "outlook",
            origin="automatic",
            gate="outlook",
            deadline=self.now,
            session=self.session.day,
        )
        with patch("time.time", return_value=self.now):
            controls.process_job()
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT status FROM signal_jobs").fetchone()[0], "cancelled"
            )
        self.assertEqual(self.box.pending(), 0)
        self.assertIsNone(self.service.repo.get())

    def test_owner_only_and_retention_schema_upgrade_preserve_ledger_and_prior_packets(self):
        self.service.issue("scheduled", now=self.now)
        Ledger(self.store).record("VST", "buy", "2", "100", "USD", "2026-11-06")
        with self.store.connection() as db:
            db.execute("PRAGMA user_version=7")
        upgraded = Store(self.store.path)
        self.assertEqual(Ledger(upgraded).held(), {"VST"})
        self.assertIsNotNone(OutlookStore(upgraded).get())

        class Sender:
            def send(self, *a, **kw):
                raise AssertionError("Unauthorized message must have no reply")

        bot = Bot(self.config, upgraded, Sender(), object())
        for text in ("/outlook now", "/outlook off", "/outlook results"):
            bot.handle(
                {
                    "message": {
                        "chat": {"id": 999, "type": "private"},
                        "from": {"id": 999},
                        "text": text,
                    }
                }
            )
        self.assertEqual(upgraded.settings()["outlook_enabled"], "on")
        with patch("time.time", return_value=self.now):
            self.service.evaluate(self.http(), now=self.session.closes + 1800)
        Maintenance(upgraded, 123).expire(self.now + 100 * 86400)
        self.assertIsNone(OutlookStore(upgraded).get())
        with upgraded.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM outlook_evaluations").fetchone()[0], 0
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)

    def test_manual_rate_payload_and_bounded_retention_are_enforced(self):
        began = self.now - 3600
        self.service.issue(now=began)
        with self.assertRaisesRegex(ValueError, "one minute"):
            self.service.issue(now=began + 1)
        for index in range(1, 34):
            self.service.issue(now=began + index * 61)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM market_outlooks WHERE kind='manual'").fetchone()[
                    0
                ],
                32,
            )
        with patch("trabe.outlook.MAX_PACKET", 500):
            with self.assertRaisesRegex(ValueError, "64 KiB"):
                self.service.issue(now=began + 35 * 61, owner_id=123)
        self.assertEqual(self.box.pending(), 0)
        self.service.repo.prune(self.now + 8 * 86400)
        self.assertIsNone(self.service.repo.get())

    def test_upgrade_from_schema_seven_adds_tables_without_rewriting_old_snapshot(self):
        from trabe.snapshots import SnapshotService

        legacy = Store(self.store.path.parent / "legacy.db")
        Ledger(legacy).record("VST", "buy", "2", "100", "USD", "2026-11-06")
        old_packet = SnapshotService(legacy).capture(now=self.now)
        with legacy.connection() as db:
            db.execute("DROP TABLE outlook_evaluations")
            db.execute("DROP TABLE market_outlooks")
            db.execute("DROP TABLE outlook_runs")
            db.execute("PRAGMA user_version=7")
        upgraded = Store(legacy.path)
        self.assertEqual(SnapshotService(upgraded).repo.get(), old_packet)
        self.assertEqual(Ledger(upgraded).held(), {"VST"})
        self.assertIsNone(OutlookStore(upgraded).get())

    def test_completed_manual_issue_is_recovered_after_open_without_resending_or_fetching(self):
        with patch("time.time", return_value=self.now):
            self.insights.handle("outlook", ["now"])
        with self.store.connection() as db:
            job = db.execute("SELECT id FROM signal_jobs").fetchone()[0]
        self.service.issue(now=self.now, request_key=f"job:{job}", owner_id=123)
        with patch("time.time", return_value=self.session.opens + 1):
            with patch.object(
                self.insights, "market_context", side_effect=AssertionError("No repeated fetch")
            ):
                self.insights.process_job()
            with self.assertRaisesRegex(ValueError, "issuance has closed"):
                self.insights.handle("outlook", ["now"])
        self.assertEqual(self.box.pending(), 1)
        self.service.schedule(self.insights, self.session.opens + 1)
        self.assertIn("missed", self.service.status())

    def test_failed_old_outcome_does_not_block_later_session_assessment(self):
        self.service.issue("scheduled", now=self.now)
        next_issue = stamp("2026-11-10T14:00:00+00:00")
        self.seed(now=next_issue, day="2026-11-10")
        self.service.issue("scheduled", now=next_issue)
        self.session = self.calendar.at(next_issue)
        http = self.http()
        now = self.session.closes + 1800
        self.service.evaluate(http, now=now)
        self.service.evaluate(http, now=now + 30)
        with self.store.connection() as db:
            assessed = json.loads(
                db.execute("SELECT payload FROM outlook_evaluations").fetchone()[0]
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM outlook_evaluations").fetchone()[0], 1
            )
        self.assertEqual(assessed["session_date"], "2026-11-10")
        self.assertEqual(len(http.calls), 4)

    def test_failed_news_refresh_reduces_session_specificity_with_retained_data(self):
        self.signals.fail("market-news", "finnhub", "*", "Market news deferred.", self.now)
        packet = self.service.issue(now=self.now)
        self.assertEqual(packet["forecast"]["opening"], "bullish")
        self.assertEqual(packet["forecast"]["session"], "insufficient")
        self.assertIn("feeds are deferred", packet["forecast"]["text"])

    def test_timed_market_preparation_takes_priority_over_long_company_news_batch(self):
        from test_market_context import chart
        from test_signals import FixtureHTTP

        class HTTP(FixtureHTTP):
            def json(self, url, provider=None):
                return chart(unquote(urlsplit(url).path.rsplit("/", 1)[-1]))

        controls = Insights(self.config, self.store, self.box, http_factory=HTTP)
        controls.queue("news", ["VST"])
        controls.queue(
            "outlook",
            origin="automatic",
            gate="outlook",
            deadline=self.now,
            session=self.session.day,
        )
        with patch("time.time", return_value=self.now - 60):
            controls.process_job()
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT kind,status FROM signal_jobs ORDER BY id").fetchall(),
                [("news", "pending"), ("outlook", "completed")],
            )
        self.assertEqual(self.box.pending(), 0)

    def test_recent_company_earnings_adds_risk_without_becoming_whole_market_sentiment(self):
        self.store.add("VST", 2)
        self.store.set_state(
            "news_view_VST",
            json.dumps(
                {
                    "as_of": self.now,
                    "evidence": [
                        {
                            "known_at": self.now - 60,
                            "published_at": self.now - 3600,
                            "url": "https://example.com/vst",
                            "headline": "VST reports earnings",
                            "input_quality": "article",
                            "analysis": {
                                "event_type": "earnings",
                                "bias": -1,
                                "method": "fixture",
                                "input_hash": "known-input",
                            },
                        }
                    ],
                }
            ),
        )
        packet = self.service.issue(now=self.now)
        self.assertEqual(packet["forecast"]["session"], "bullish")
        self.assertIn("company earnings", packet["forecast"]["catalysts"])
        evidence = packet["inputs"]["company_news"][0]["evidence"][0]
        self.assertEqual(evidence["analysis"]["input_hash"], "known-input")
        self.assertEqual(evidence["url"], "https://example.com/vst")

    def test_vix_prior_close_uses_actual_index_close_after_equities_including_early_close(self):
        # Saved Yahoo/Cboe evidence: Friday 2026-11-06 at 16:15:01 New York.
        self.seed(vix="25.64")
        packet = self.service.issue(now=self.now)
        value = packet["inputs"]["market"]["^VIX"]
        self.assertEqual(value["value"]["regular_time"], 1793999701)
        self.assertTrue(value["usable"])
        self.assertEqual(packet["forecast"]["session"], "bullish")
        previous = self.calendar.previous(date(2026, 11, 9))
        vix = self.signals.get("market-context", "^VIX")["payload"]
        vix["regular_time"] = previous.closes  # Still before the index close.
        self.signals.put("market-context", "^VIX", "^VIX", "yahoo-chart", vix, now=self.now + 61)
        packet = self.service.issue(now=self.now + 61)
        self.assertEqual(packet["forecast"]["session"], "insufficient")
        monday = stamp("2026-11-30T14:00:00+00:00")
        self.seed(now=monday, day="2026-11-30")
        packet = self.service.issue(now=monday)
        self.assertTrue(packet["inputs"]["market"]["^VIX"]["usable"])
        self.assertEqual(
            datetime.fromtimestamp(
                packet["inputs"]["market"]["^VIX"]["value"]["regular_time"], NY
            ).hour,
            13,
        )


if __name__ == "__main__":
    unittest.main()
