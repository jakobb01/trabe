"""Expectations, comparable EPS history and durable partial-result updates."""

import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from trabe.earnings import Earnings
from trabe.earnings_results import (
    DAY,
    RESULT_INTERVAL,
    comparison,
    metric_line,
    number,
    result_window,
)
from trabe.signals import SignalStore, WorkStopped
from trabe.store import Store


class EarningsResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "app.db")
        self.store.add("VST", 2)
        self.store.set_setting("earnings_enabled", "on")
        self.now = datetime(2026, 11, 6, 12, 30, tzinfo=UTC).timestamp()
        self.repo = SignalStore(self.store)
        self.calendar = {
            "symbol": "VST",
            "date": "2026-11-06",
            "year": 2026,
            "quarter": 3,
            "hour": "bmo",
            "epsEstimate": 2,
            "epsActual": None,
            "revenueEstimate": 6_000_000_000,
            "revenueActual": None,
        }
        self.history = [
            {
                "symbol": "VST",
                "period": "2026-06-30",
                "year": 2026,
                "quarter": 2,
                "actual": 1,
                "estimate": 2,
            },
            {
                "symbol": "VST",
                "period": "2026-03-31",
                "year": 2026,
                "quarter": 1,
                "actual": 3,
                "estimate": 2,
            },
            {
                "symbol": "VST",
                "period": "2025-12-31",
                "year": 2025,
                "quarter": 4,
                "actual": 2,
                "estimate": 2,
            },
            {
                "symbol": "VST",
                "period": "2025-09-30",
                "year": 2025,
                "quarter": 3,
                "actual": None,
                "estimate": 2,
            },
        ]
        self.calls = []
        self.history_error = None
        fixture = self

        class HTTP:
            def finnhub(self, endpoint, **params):
                fixture.calls.append(endpoint)
                if endpoint == "stock/earnings":
                    if fixture.history_error:
                        raise fixture.history_error
                    return copy.deepcopy(fixture.history)
                return {"earningsCalendar": [copy.deepcopy(fixture.calendar)]}

        self.http = HTTP()
        self.service = Earnings(self.store, self.http)

    def updates(self, now):
        return [
            (key, text)
            for key, text in self.service.alerts(["VST"], now=now)
            if key.startswith("result:")
        ]

    def claim(self, updates, now):
        for key, text in updates:
            self.assertTrue(
                self.repo.notify_once(
                    123, "earnings", key, text, now=now, setting="earnings_enabled"
                )
            )

    def enable_alpha(self):
        # Parser tests only: this feed is quarantined by default in production.
        self.store.set_state("earnings_alpha_disabled", "0")
        self.alpha = json.loads(
            (Path(__file__).parent / "fixtures/earnings_vst_alpha.json").read_text()
        )
        self.alpha_error = None
        self.http.config = SimpleNamespace(api_key="alpha-fixture")

        def fetch(function, **params):
            self.calls.append(function)
            if self.alpha_error:
                raise self.alpha_error
            return copy.deepcopy(self.alpha)

        self.http.alpha_json = fetch

    def test_alpha_parser_keeps_provider_pair_and_exact_period_revenue(self):
        self.enable_alpha()
        self.service.refresh(["VST"], now=self.now)
        payload = self.repo.rows("earnings", ["VST"])[0]["payload"]
        self.assertTrue(payload["reported"])
        self.assertEqual(payload["comparisons"]["eps"]["estimate"], "2.94")
        self.assertEqual(payload["comparisons"]["revenue"]["actual"], "6347000000")
        self.assertIsNone(payload["comparisons"]["revenue"]["estimate"])
        self.assertIsNone(payload["comparisons"]["revenue"]["currency"])
        report = self.service.report(["VST"], now=self.now)
        self.assertIn("EPS: 1.75 vs 2.94 expected", report)
        self.assertIn("Revenue: 6.35B vs unavailable expected", report)
        self.assertIn("currency unavailable", report)
        self.assertIn("Alpha Vantage fallback", report)
        updates = self.updates(self.now)
        self.assertEqual(len(updates), 1)
        self.claim(updates, self.now)
        self.service = Earnings(Store(self.store.path), self.http)
        self.service.refresh(["VST"], now=self.now + RESULT_INTERVAL)
        self.assertEqual(self.updates(self.now + RESULT_INTERVAL), [])
        self.assertEqual(self.calls.count("EARNINGS"), 1)
        # A later complete primary response takes precedence over the fallback.
        self.calendar.update(epsActual=2.5, revenueActual=6_000_000_000)
        self.service.refresh(["VST"], now=self.now + 2 * RESULT_INTERVAL)
        self.assertIn(
            "EPS: 2.5 vs 2 expected",
            self.service.report(["VST"], now=self.now + 2 * RESULT_INTERVAL),
        )

    def test_alpha_mismatched_dates_or_symbols_do_not_attach_or_mix_figures(self):
        self.enable_alpha()
        self.alpha["quarterlyEarnings"][0]["reportedDate"] = "2026-11-05"
        self.service.refresh(["VST"], now=self.now)
        self.assertFalse(self.repo.rows("earnings", ["VST"])[0]["payload"]["reported"])
        self.assertNotIn("INCOME_STATEMENT", self.calls)
        self.alpha["symbol"] = "OTHER"
        self.service.refresh(["VST"], now=self.now + 1, force=True)
        self.assertFalse(self.repo.rows("earnings", ["VST"])[0]["payload"]["reported"])

    def test_alpha_failure_retains_primary_cache_and_cancellation_propagates(self):
        self.enable_alpha()
        self.alpha_error = OSError("private diagnostic")
        self.service.refresh(["VST"], now=self.now)
        self.assertEqual(len(self.service.results.history("VST")), 4)
        self.assertFalse(self.repo.rows("earnings", ["VST"])[0]["payload"]["reported"])
        status = next(s for s in self.repo.status("earnings") if s["source"] == "alphavantage-eps")
        self.assertIn("unavailable", status["error"])
        self.alpha_error = WorkStopped("cancelled")
        with self.assertRaises(WorkStopped):
            self.service.refresh(["VST"], now=self.now + 1, force=True)

    def test_fallback_receipt_time_prevents_backdating_new_figures(self):
        self.enable_alpha()
        fetch = self.http.alpha_json

        def delayed(function, **params):
            self.http.received_at = self.http.alpha_checked_at = self.now + 15
            return fetch(function, **params)

        self.http.alpha_json = delayed
        self.service.refresh(["VST"], now=self.now)
        row = self.repo.rows("earnings", ["VST"])[0]
        self.assertEqual(row["last_seen"], self.now + 15)
        self.assertEqual(row["payload"]["result_seen"]["eps"], self.now + 15)

    def test_quarantined_alpha_feed_never_populates_or_grades_results(self):
        self.enable_alpha()
        self.store.set_state("earnings_alpha_disabled", "1")
        self.service.refresh(["VST"], now=self.now, force=True)
        self.assertNotIn("EARNINGS", self.calls)
        self.assertFalse(self.repo.rows("earnings", ["VST"])[0]["payload"]["reported"])

    def test_report_has_expectations_four_quarters_and_distinct_outcomes_without_links(self):
        self.service.refresh(["VST"], now=self.now)
        report = self.service.report(["VST"], now=self.now)
        self.assertIn("Expected: EPS 2 • Revenue 6.00B", report)
        self.assertIn("FY2026 Q2 • 🔴 MISS", report)
        self.assertIn("FY2026 Q1 • 🟢 HIT", report)
        self.assertIn("FY2025 Q4 • ⚪ MET", report)
        self.assertIn("EPS: unavailable vs 2 expected", report)
        self.assertNotIn("https://", report)
        before = len(self.calls)
        self.service.report(["VST"], now=self.now + 1)
        self.service.briefing(["VST"], now=self.now + 1)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.updates(self.now), [])

    def test_comparisons_handle_losses_zero_missing_and_invalid_values(self):
        self.assertEqual(comparison("-0.5", "-1"), ("HIT", Decimal(50)))
        self.assertEqual(comparison("0", "0"), ("MET", None))
        self.assertIsNone(comparison("1", None))
        for invalid in (True, "NaN", "Infinity", "", "1e99"):
            self.assertIsNone(number(invalid))
        self.assertIsNone(number("-1", revenue=True))
        self.assertIn("% unavailable", metric_line("eps", {"actual": "0", "estimate": "0"}))

    def test_partial_results_notify_only_new_metrics_and_survive_restart(self):
        self.service.refresh(["VST"], now=self.now)
        self.calendar["epsActual"] = 2.5
        later = self.now + RESULT_INTERVAL
        self.service.refresh(["VST"], now=later)
        updates = self.updates(later)
        self.assertEqual(len(updates), 1)
        self.assertIn("HIT • EPS: 2.5 vs 2 expected (+25.0%)", updates[0][1])
        self.assertIn("Revenue: awaiting", updates[0][1])
        self.claim(updates, later)
        self.service = Earnings(Store(self.store.path), self.http)
        self.assertEqual(self.updates(later + 1), [])
        self.calendar["revenueActual"] = 5_500_000_000
        self.service.refresh(["VST"], now=later + RESULT_INTERVAL)
        updates = self.updates(later + RESULT_INTERVAL)
        self.assertEqual(len(updates), 1)
        self.assertIn("MISS • Revenue", updates[0][1])
        self.assertNotIn("EPS:", updates[0][1])
        self.claim(updates, later + RESULT_INTERVAL)
        self.store.remove("VST")
        self.store.add("VST", 1)
        self.assertEqual(self.updates(later + RESULT_INTERVAL + 1), [])

    def test_complete_results_are_combined_and_do_not_repeat_after_estimate_edit(self):
        self.service.refresh(["VST"], now=self.now)
        self.calendar.update(epsActual=2, revenueActual=6_000_000_000)
        later = self.now + RESULT_INTERVAL
        self.service.refresh(["VST"], now=later)
        updates = self.updates(later)
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0][0].endswith(":eps+revenue"))
        self.assertIn("MET • EPS", updates[0][1])
        self.assertIn("MET • Revenue", updates[0][1])
        self.claim(updates, later)
        self.calendar["epsEstimate"] = 1
        self.service.refresh(["VST"], now=later + 1, force=True)
        self.assertEqual(self.updates(later + 1), [])
        self.assertEqual(self.service._refresh_interval("VST", later + 1), DAY)

    def test_history_uses_matching_fiscal_period_and_its_own_estimate(self):
        self.calendar["year"] = 2027  # Fiscal year differs from release calendar year.
        self.service.refresh(["VST"], now=self.now)
        self.history.insert(
            0,
            {
                "symbol": "VST",
                "period": "2026-09-30",
                "year": 2027,
                "quarter": 3,
                "actual": 2.5,
                "estimate": 2.25,
            },
        )
        later = self.now + RESULT_INTERVAL
        self.service.refresh(["VST"], now=later)
        updates = self.updates(later)
        self.assertEqual(len(updates), 1)
        self.assertIn("FY2027 Q3", updates[0][1])
        self.assertIn("2.5 vs 2.25 expected (+11.1%)", updates[0][1])
        self.assertIn("reported results", self.service.report(["VST"], now=later))
        self.assertEqual(len(self.service.results.history("VST")), 4)

    def test_first_fetch_seeds_reported_data_without_old_result_alerts(self):
        self.calendar.update(epsActual=2.5, revenueActual=6_100_000_000)
        self.service.refresh(["VST"], now=self.now)
        self.assertEqual(self.updates(self.now), [])
        self.assertIn("HIT • EPS", self.service.report(["VST"], now=self.now))

    def test_activation_pause_and_resume_suppress_older_results(self):
        self.service.refresh(["VST"], now=self.now)
        self.calendar["epsActual"] = 2.5
        later = self.now + RESULT_INTERVAL
        self.service.refresh(["VST"], now=later)
        self.store.pause_automation(later + 600)
        self.assertEqual(self.updates(later), [])
        self.store.pause_automation(0)
        self.store.set_state("insights_resumed_at", str(later + 1))
        self.assertEqual(self.updates(later + 2), [])
        self.store.set_state("insights_resumed_at", "0")
        self.store.set_state("earnings_enabled_at", str(later + 1))
        self.assertEqual(self.updates(later + 2), [])

    def test_adaptive_polls_are_cached_until_due_and_limited_to_release_window(self):
        self.service.refresh(["VST"], now=self.now)
        self.assertEqual(self.service._refresh_interval("VST", self.now), RESULT_INTERVAL)
        count = len(self.calls)
        self.service.refresh(["VST"], now=self.now + 60)
        self.assertEqual(len(self.calls), count)
        self.service.refresh(["VST"], now=self.now + RESULT_INTERVAL)
        self.assertEqual(len(self.calls), count + 2)
        self.assertFalse(result_window(self.calendar, self.now - 1))
        self.assertFalse(result_window(self.calendar, self.now + 8 * DAY))
        after_close = self.calendar | {"hour": "after-close"}
        self.assertFalse(result_window(after_close, self.now))
        self.assertTrue(
            result_window(after_close, datetime(2026, 11, 6, 21, tzinfo=UTC).timestamp())
        )

    def test_history_failure_preserves_cache_and_cancellation_stops_next_call(self):
        self.service.refresh(["VST"], now=self.now)
        prior = self.service.results.history("VST")
        self.history_error = OSError("secret provider diagnostic")
        self.service.refresh(["VST"], now=self.now + RESULT_INTERVAL)
        self.assertEqual(self.service.results.history("VST"), prior)
        self.assertEqual(self.updates(self.now + RESULT_INTERVAL), [])
        self.assertNotIn(
            "secret provider", self.service.report(["VST"], now=self.now + RESULT_INTERVAL)
        )
        self.history_error = WorkStopped("stopped")
        with self.assertRaises(WorkStopped):
            self.service.refresh(["VST"], now=self.now + 2 * RESULT_INTERVAL, force=True)

    def test_pending_release_stays_visible_after_its_scheduled_window(self):
        self.service.refresh(["VST"], now=self.now)
        during_session = datetime(2026, 11, 6, 16, tzinfo=UTC).timestamp()
        report = self.service.report(["VST"], now=during_session)
        briefing = "\n".join(self.service.briefing(["VST"], now=during_session))
        for message in (report, briefing):
            self.assertTrue("Results pending" in message or "Results not confirmed" in message)
            self.assertIn("EPS 2", message)
            self.assertIn("Revenue 6.00B", message)
        self.assertEqual(self.updates(during_session), [])

    def test_delayed_result_is_checked_and_visible_four_days_later(self):
        self.service.refresh(["VST"], now=self.now)
        later = datetime(2026, 11, 10, 16, tzinfo=UTC).timestamp()
        self.assertEqual(self.service._refresh_interval("VST", later), 3600)
        self.assertIn("Results not confirmed", self.service.report(["VST"], now=later))
        self.calendar["epsActual"] = 2.5
        self.service.refresh(["VST"], now=later)
        self.assertIn("reported results", self.service.report(["VST"], now=later))

    def test_live_vst_missing_provider_figures_are_not_a_parsing_failure(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/earnings_vst_provider_gap.json").read_text()
        )
        now = datetime.fromisoformat(fixture["recorded_at"]).timestamp()
        self.calendar = fixture["calendar"]["earningsCalendar"][0]
        self.history = fixture["history"]
        self.service.refresh(["VST"], now=now, force=True)
        self.assertEqual(self.calls, ["calendar/earnings", "stock/earnings"])
        report = self.service.report(["VST"], now=now)
        self.assertIn("Results not confirmed", report)
        self.assertIn("FY2026 Q2", report)
        self.assertFalse(self.repo.rows("earnings", ["VST"])[0]["payload"]["reported"])

        # Synthetic later response: prove the same matching code accepts Q3
        # once that quarter exists, rather than reusing last year's Q3.
        self.history.insert(
            0,
            {
                "symbol": "VST",
                "period": "2026-09-30",
                "year": 2026,
                "quarter": 3,
                "actual": 3,
                "estimate": 2.5,
            },
        )
        self.service.refresh(["VST"], now=now + RESULT_INTERVAL, force=True)
        report = self.service.report(["VST"], now=now + RESULT_INTERVAL)
        self.assertIn("FY2026 Q3 • reported results", report)
        self.assertIn("EPS: 3 vs 2.5 expected", report)
        self.assertNotIn("Results not confirmed", report)

    def test_zero_eps_is_a_valid_reported_result_not_missing_data(self):
        self.calendar["epsEstimate"] = 0
        self.service.refresh(["VST"], now=self.now)
        self.calendar["epsActual"] = 0
        self.service.refresh(["VST"], now=self.now + RESULT_INTERVAL)
        updates = self.updates(self.now + RESULT_INTERVAL)
        self.assertEqual(len(updates), 1)
        self.assertIn("MET • EPS: 0 vs 0 expected", updates[0][1])
