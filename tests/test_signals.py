"""Shared signals: preserved state, quotas, owner controls and atomic delivery."""

import io
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from trabe.api import ApiError, MarketData, message_chunks
from trabe.bot import Bot
from trabe.config import Config
from trabe.insights import Insights
from trabe.ledger import Ledger
from trabe.outbox import Outbox
from trabe.signals import SignalHTTP, SignalStore, WorkStopped
from trabe.store import Store


class FixtureHTTP:
    def __init__(self, config=None, store=None, **kwargs):
        self.store, self.kwargs = store, kwargs

    def finnhub(self, endpoint, **params):
        if endpoint == "stock/earnings":
            return []
        if endpoint == "calendar/earnings":
            return {
                "earningsCalendar": [
                    {
                        "symbol": params["symbol"],
                        "date": "2026-11-06",
                        "year": 2026,
                        "quarter": 3,
                        "hour": "bmo",
                    }
                ]
            }
        if endpoint == "company-news":
            return [
                {
                    "id": 1,
                    "headline": "Company raises guidance",
                    "summary": "The company raised its forecast. Revenue rose during the quarter.",
                    "datetime": datetime(2026, 11, 4, 11, tzinfo=UTC).timestamp(),
                    "url": "https://example.com/news",
                    "source": "Fixture",
                    "related": params["symbol"],
                }
            ]
        return {"data": []}

    def json(self, url, provider=None):
        return {"data": []}

    def alpha_csv(self, *args, **kwargs):
        return "symbol,reportDate,fiscalDateEnding\n"


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "app.db")
        self.store.add("VST", 2)
        self.config = Config(
            "100:" + "a" * 32, 123, "alpha-fixture", self.store.path, "finnhub-fixture"
        )
        self.repo = SignalStore(self.store)
        self.box = Outbox(self.store)
        self.now = datetime(2026, 11, 4, 12, tzinfo=UTC).timestamp()
        self.insights = Insights(self.config, self.store, self.box, http_factory=FixtureHTTP)

    def test_signal_update_preserves_first_availability_and_bounds_payload(self):
        self.repo.put("news", "a", "VST", "fixture", {"x": 1}, now=10, event_time=9)
        previous = self.repo.put("news", "a", "VST", "other", {"x": 2}, now=20, event_time=9)
        self.assertEqual(previous["observed_at"], 10)
        current = self.repo.get("news", "a")
        self.assertEqual(
            (current["observed_at"], current["available_at"], current["last_seen"]), (10, 10, 20)
        )
        with self.assertRaises(ValueError):
            self.repo.put("news", "large", "VST", "fixture", {"text": "a" * 40000}, now=20)

    def test_claim_and_outbox_are_atomic_and_pause_is_rechecked(self):
        self.store.set_setting("earnings_enabled", "on")
        with patch("trabe.outbox.Outbox.send", side_effect=ValueError("full")):
            with self.assertRaises(ValueError):
                self.repo.notify_once(
                    123, "earnings", "event", "warning", now=self.now, setting="earnings_enabled"
                )
        self.assertFalse(self.repo.notified("earnings", "event"))
        self.assertTrue(
            self.repo.notify_once(
                123, "earnings", "event", "warning", now=self.now, setting="earnings_enabled"
            )
        )
        self.assertFalse(
            self.repo.notify_once(
                123, "earnings", "event", "warning", now=self.now, setting="earnings_enabled"
            )
        )
        self.store.pause_automation(self.now + 600)
        self.assertFalse(
            self.repo.notify_once(
                123, "earnings", "next", "warning", now=self.now, setting="earnings_enabled"
            )
        )
        self.assertEqual(self.box.pending(), 0)

    def test_feature_off_drops_proactive_delivery_but_keeps_manual_reply(self):
        self.box.send(123, "disabled earnings", kind="earnings")
        self.box.send(123, "manual reply")
        messages = []

        class Sender:
            def send(self, chat_id, text, **kwargs):
                messages.append(text)

        self.box.flush(Sender())
        self.assertEqual(messages, ["manual reply"])

    def test_new_http_arrays_share_finnhub_usage_and_ignore_monitor_switch(self):
        self.store.set_setting("news_enabled", "on")
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return io.BytesIO(b'[{"headline":"item"}]')

        http = SignalHTTP(self.config, self.store, automatic=True, feature="news", opener=opener)
        self.assertEqual(http.finnhub("company-news", symbol="VST"), [{"headline": "item"}])
        self.assertEqual(self.store.provider_usage("finnhub"), 1)
        self.assertEqual(self.store.usage(), 0)
        self.assertNotIn("finnhub-fixture", calls[0].full_url)
        self.assertEqual(calls[0].get_header("X-finnhub-token"), "finnhub-fixture")
        self.store.pause_automation(datetime.now(UTC).timestamp() + 600)
        with self.assertRaises(WorkStopped):
            http.finnhub("company-news", symbol="VST")
        self.assertEqual(len(calls), 1)

    def test_public_transport_rejects_unapproved_hosts(self):
        with self.assertRaises(ValueError):
            SignalHTTP(self.config, self.store).json(
                "https://example.com/data", provider="tracefour"
            )

    def test_alpha_csv_rejects_bad_responses_and_shares_one_cached_calendar(self):
        responses = iter(
            (
                b"<html>unavailable</html>",
                b'\xef\xbb\xbf{"Information":"API request limit reached"}',
                b"symbol,reportDate,fiscalDateEnding\nVST,2026-11-06,2026-09-30\n",
            )
        )
        http = SignalHTTP(
            self.config, self.store, opener=lambda *a, **kw: io.BytesIO(next(responses))
        )
        with patch.object(http, "_pace"):
            with self.assertRaisesRegex(ApiError, "invalid calendar CSV"):
                http.alpha_csv("EARNINGS_CALENDAR", horizon="3month")
            with self.assertRaises(ApiError) as raised:
                http.alpha_csv("EARNINGS_CALENDAR", horizon="3month")
            self.assertEqual(raised.exception.code, 429)
            self.assertGreater(float(self.store.state("market_retry_after")), 0)
            with self.store.connection() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM signal_http_cache").fetchone()[0], 0
                )
            self.store.set_state("market_retry_after", "0")
            calendar = http.alpha_csv("EARNINGS_CALENDAR", horizon="3month")
            self.assertIn("VST", calendar)
            self.assertEqual(http.alpha_csv("EARNINGS_CALENDAR", horizon="3month"), calendar)
        self.assertEqual(self.store.usage(), 3)

    def test_alpha_fundamentals_share_budget_cache_and_reject_error_notices(self):
        responses = iter(
            (
                b'{"symbol":"VST","quarterlyEarnings":[]}',
                b'{"Information":"API request limit reached"}',
            )
        )
        http = SignalHTTP(
            self.config, self.store, opener=lambda *a, **kw: io.BytesIO(next(responses))
        )
        with patch.object(http, "_pace"):
            self.assertEqual(http.alpha_json("EARNINGS", symbol="VST")["symbol"], "VST")
            self.assertEqual(http.alpha_json("EARNINGS", symbol="VST")["symbol"], "VST")
            self.assertEqual(self.store.usage(), 1)
            with self.assertRaises(ApiError) as raised:
                http.alpha_json("INCOME_STATEMENT", symbol="VST")
            self.assertEqual(raised.exception.code, 429)
        self.assertEqual(self.store.usage(), 2)

    def test_schema_upgrade_preserves_portfolio_and_retention_does_not_purge_ledger(self):
        ledger = Ledger(self.store)
        ledger.record("VST", "buy", "2", "100", "EUR", "2026-01-02")
        self.store.set_setting("default_currency", "USD")
        with self.store.connection() as db:
            for table in (
                "signals",
                "signal_sources",
                "signal_notifications",
                "signal_jobs",
                "signal_http_calls",
                "signal_http_cache",
            ):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=4")
        upgraded = Store(self.store.path)
        from trabe.maintenance import Maintenance

        old = self.now - 120 * 86400
        self.repo.put("insiders", "old", "VST", "fixture", {}, now=self.now, event_time=old)
        self.repo.put(
            "earnings", "future", "VST", "fixture", {}, now=old, event_time=self.now + 86400
        )
        self.store.set_state("briefing_wait_2026-01-01", str(old))
        Maintenance(upgraded).run_once(now=self.now)
        self.assertIsNone(self.repo.get("insiders", "old"))
        self.assertIsNotNone(self.repo.get("earnings", "future"))
        self.assertEqual(self.store.state("briefing_wait_2026-01-01"), "")
        self.assertEqual(upgraded.settings()["default_currency"], "USD")
        self.assertEqual(Ledger(upgraded).positions("EUR")["VST"][1], 200)
        self.assertEqual(upgraded.watchlist(), [("VST", 2)])

    def test_cached_owner_controls_queue_missing_symbol_without_fetching(self):
        bot = Bot(
            self.config,
            self.store,
            self.box,
            MarketData("fixture", self.store),
            insights=self.insights,
        )
        update = {
            "update_id": 1,
            "message": {
                "text": "/earnings on",
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999},
            },
        }
        bot.process(update)
        self.assertEqual(self.store.settings()["earnings_enabled"], "off")
        self.assertIn("queued", bot.command("news", ["GOOGL"]))
        self.assertEqual(self.store.provider_usage("finnhub"), 0)
        with patch("trabe.insights.time.time", return_value=self.now):
            self.insights.process_job()
            self.assertIn(
                "Company raises guidance", self.insights.handle("news", ["cached", "GOOGL"])
            )
        self.assertIn("queued", self.insights.handle("news", ["NVDA"]))

    def test_default_reports_refresh_and_cached_reports_never_queue(self):
        for kind in ("news", "insiders", "earnings"):
            self.assertIn("queued", self.insights.handle(kind, []))
            self.insights.process_job()

            before = self.box.pending()
            self.insights.handle(kind, ["cached", "VST"])
            self.assertFalse(self.insights.process_job())
            self.assertEqual(self.box.pending(), before)
            self.assertIn("queued", self.insights.handle(kind, ["VST"]))
            self.insights.process_job()

    def test_singular_insider_alias_uses_same_refresh_and_cached_controls(self):
        self.assertIn("queued", self.insights.handle("insider", ["VST"]))
        self.insights.process_job()
        self.assertIn("Insider purchases", self.insights.handle("insider", ["cached", "VST"]))

    def test_news_job_reports_progress_and_finishes_when_optional_provider_fails(self):
        from dataclasses import replace

        self.store.add("GOOGL", 2)
        self.store.set_setting("news_sources", "finnhub,massive")
        config = replace(self.config, massive_key="massive-fixture")
        observations = []
        parent = self

        class FailingSecondSource(FixtureHTTP):
            def __init__(self, config, store, **kwargs):
                super().__init__(config, store, **kwargs)
                self.config = config

            def news_api(self, *args, **kwargs):
                observations.append(parent.insights.status())
                raise ApiError("Signal provider network request failed.")

        self.insights = Insights(config, self.store, self.box, http_factory=FailingSecondSource)
        self.insights.queue("news")
        self.insights.process_job()
        self.assertIn("1/2 checked", self.insights.status())
        self.assertEqual(self.box.pending(), 0)
        self.insights.process_job()
        self.assertEqual(self.box.pending(), 1)
        self.assertTrue(all("running: 1" in status for status in observations))
        self.assertIn("Massive unavailable", self.insights.handle("news", ["sources"]))
        with self.store.connection() as db:
            state, raw = db.execute(
                "SELECT status,symbols FROM signal_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(state, "completed")
        self.assertEqual(__import__("json").loads(raw)["remaining"], [])

    def test_briefing_job_combines_sections_without_enabling_automation(self):
        self.assertIn("queued", self.insights.handle("briefing", ["now"]))
        with patch("trabe.insights.time.time", return_value=self.now):
            self.insights.process_job()
        with self.store.connection() as db:
            message = "\n".join(r[0] for r in db.execute("SELECT text FROM outbox"))
        for expected in (
            "Positions",
            "Earnings",
            "Insider purchases",
            "/news cached",
            "/briefing details",
            "/outlook results",
        ):
            self.assertIn(expected, message)
        self.assertNotIn("Watchlist prices", message)
        self.assertEqual(self.store.settings()["briefing_enabled"], "off")

    def test_cached_overview_and_details_do_not_queue_or_fetch(self):
        with (
            patch("trabe.insights.time.time", return_value=self.now),
            patch.object(FixtureHTTP, "finnhub", side_effect=AssertionError("network")),
            patch.object(FixtureHTTP, "json", side_effect=AssertionError("network")),
        ):
            overview = self.insights.handle("briefing", ["cached"])
            details = self.insights.handle("briefing", ["details"])
        self.assertIn("SPY", overview)
        self.assertIn("QQQ", overview)
        self.assertIn("VIX: unavailable", overview)
        self.assertNotIn("Watchlist prices", overview)
        self.assertIn("Watchlist prices", details)
        self.assertIn("VST", details)
        self.assertIn("News", details)
        self.assertEqual(len(list(message_chunks(overview))), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM signal_jobs").fetchone()[0], 0)
        self.assertEqual(self.box.pending(), 0)
        self.assertEqual(self.store.settings()["briefing_enabled"], "off")

    def test_overview_keeps_source_failures_with_retained_cards(self):
        for kind in ("market-news", "earnings", "insiders"):
            self.repo.fail(kind, "fixture", "*", "Unavailable", self.now)
        text = self.insights.render_briefing(["VST"], self.now)
        self.assertIn("Market news refresh failed", text)
        self.assertIn("Some source requests failed", text)
        self.assertIn("retained data may be stale", text)

    def test_long_cards_are_omitted_whole_and_details_remain_available(self):
        card = "VST result " + "📈" * 1800 + " • unverified comparison"
        with patch("trabe.earnings.Earnings.briefing", return_value=[card]):
            overview = self.insights.render_briefing(["VST"], self.now)
            details = self.insights.render_briefing_details(["VST"], self.now)
        self.assertEqual(len(list(message_chunks(overview))), 1)
        self.assertNotIn("VST result", overview)
        self.assertIn("Some sections exceed", overview)
        self.assertIn("/briefing details", overview)
        self.assertIn("/outlook results", overview)
        self.assertIn(card, details)

    def test_overview_labels_stale_and_missing_position_values(self):
        self.store.set_setting("default_currency", "USD")
        ledger = Ledger(self.store)
        ledger.record("VST", "buy", "2", "100", "USD", "2026-11-03")
        ledger.record("GOOGL", "buy", "1", "100", "USD", "2026-11-03")
        self.store.save_live(
            dict(
                ticker="VST",
                provider="finnhub",
                currency="USD",
                price=120,
                source_time=self.now - 3600,
                received_at=self.now - 3600,
            )
        )
        text = self.insights.render_briefing(["VST", "GOOGL"], self.now)
        self.assertIn("observed value 240.00 USD", text)
        self.assertIn("unrealized +40.00 USD", text)
        self.assertIn("partial; missing values excluded", text)
        self.assertIn("stale position prices", text)

    def test_scheduled_briefing_deduplicates_and_skips_holidays(self):
        self.insights.handle("briefing", ["on"])
        self.insights.scheduled_briefing(["VST"], self.now)
        self.assertEqual(self.box.pending(), 0)  # cold-cache warm-up
        self.insights.scheduled_briefing(["VST"], self.now + 301)
        count = self.box.pending()
        self.assertGreater(count, 0)
        self.insights.scheduled_briefing(["VST"], self.now + 400)
        self.assertEqual(self.box.pending(), count)
        holiday = datetime(2026, 11, 26, 12, tzinfo=UTC).timestamp()
        self.insights.scheduled_briefing(["VST"], holiday)
        self.assertEqual(self.box.pending(), count)

    def test_late_restart_does_not_send_a_morning_briefing_after_market_open(self):
        self.insights.handle("briefing", ["on"])
        after_open = datetime(2026, 11, 4, 15, tzinfo=UTC).timestamp()
        self.insights.scheduled_briefing(["VST"], after_open)
        self.insights.scheduled_briefing(["VST"], after_open + 301)
        self.assertEqual(self.box.pending(), 0)
        self.assertIn("queued", self.insights.handle("briefing", ["now"]))

    def test_background_off_makes_no_automatic_requests_and_manual_works_paused(self):
        self.assertFalse(self.insights.run_once(self.now))
        self.assertEqual(self.repo.status("news"), [])
        self.store.pause_automation(self.now + 100)
        self.insights.queue("earnings", ["VST"])
        with patch("trabe.insights.time.time", return_value=self.now):
            self.assertTrue(self.insights.run_once(self.now))
        self.assertIn("2026-11-06", self.insights.handle("earnings", ["cached", "VST"]))


if __name__ == "__main__":
    unittest.main()
