"""Focused earnings schedules, fallback, revisions and durable warning checks."""

import io
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trabe.api import ApiError
from trabe.earnings import Earnings
from trabe.ledger import Ledger
from trabe.signals import SignalHTTP, SignalStore, WorkStopped
from trabe.store import Store


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.day = "2026-11-06"
        self.year = 2026
        self.primary_error = None
        self.csv = "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\nVST,Vistra,2026-11-06,2026-09-30,1.0,USD,pre-market\n"
        self.actual = None
        self.hour = "bmo"
        self.extra = []

    def finnhub(self, endpoint, **params):
        self.calls.append(("finnhub", endpoint, params))
        if self.primary_error:
            raise self.primary_error
        return {
            "earningsCalendar": [
                {
                    "symbol": params["symbol"],
                    "date": self.day,
                    "year": self.year,
                    "quarter": 3,
                    "hour": self.hour,
                    "epsActual": self.actual,
                },
                *self.extra,
            ]
        }

    def alpha_csv(self, function, **params):
        self.calls.append(("alphavantage", function, params))
        return self.csv


class EarningsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.store = Store(self.path)
        self.store.add("VST", 2)
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('earnings_enabled','on')")
            db.execute("INSERT OR REPLACE INTO settings VALUES ('earnings_warning_days','5')")
        self.http = FakeHTTP()
        self.service = Earnings(self.store, self.http)
        self.signals = SignalStore(self.store)
        self.now = datetime(2026, 11, 3, 12, tzinfo=UTC).timestamp()

    def emit(self, now=None):
        now = self.now if now is None else now
        notices = self.service.alerts(["VST"], now=now)
        for key, text in notices:
            self.signals.notify_once(
                123, "earnings", key, text, now=now, setting="earnings_enabled"
            )
        return notices

    def test_symbol_filter_fiscal_year_and_cache(self):
        self.http.year = 2027
        self.http.extra = [{"symbol": "OTHER", "date": self.http.day, "year": 2026, "quarter": 3}]
        self.service.refresh(["VST"], now=self.now)
        self.service.refresh(["VST"], now=self.now + 10)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.http.calls[0][2]["symbol"], "VST")
        row = self.signals.rows("earnings", ["VST"])[0]
        self.assertEqual(row["payload"]["period"], "FY2027 Q3")
        self.assertEqual(row["payload"]["hour"], "before-open")
        self.assertIn("provider-reported", self.service.report(["VST"], now=self.now))

    def test_first_observation_does_not_precede_the_http_receipt(self):
        self.http.received_at = self.now + 4
        self.service.refresh(["VST"], now=self.now)
        row = self.signals.rows("earnings", ["VST"])[0]
        self.assertEqual(row["observed_at"], self.now + 4)
        self.assertEqual(row["available_at"], self.now + 4)
        self.assertEqual(row["payload"]["checked_at"], self.now + 4)

    def test_warning_dedup_survives_restart_lead_edit_and_fallback(self):
        self.service.refresh(["VST"], now=self.now)
        self.assertEqual(len(self.emit()), 1)
        self.service = Earnings(Store(self.path), self.http)
        self.store.remove("VST")
        self.store.add("VST", 1)
        with self.store.connection() as db:
            db.execute("UPDATE settings SET value='3' WHERE key='earnings_warning_days'")
        self.assertEqual(self.emit(self.now + 60), [])
        self.http.primary_error = ApiError("unavailable", 403)
        self.service.refresh(["VST"], now=self.now + 120, force=True)
        self.assertEqual(self.emit(self.now + 120), [])
        rows = self.signals.rows("earnings", ["VST"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["payload"]["aliases"]), 2)

    def test_confirmed_period_revision_warns_once_outside_lead_window(self):
        self.service.refresh(["VST"], now=self.now)
        self.emit()
        self.http.day = "2026-11-12"
        self.service.refresh(["VST"], now=self.now + 60, force=True)
        notices = self.emit(self.now + 60)
        self.assertEqual(len(notices), 1)
        self.assertIn("2026-11-06 → 2026-11-12", notices[0][1])
        self.assertEqual(self.emit(self.now + 61), [])

    def test_date_returning_to_original_tracks_the_latest_delivered_revision(self):
        self.service.refresh(["VST"], now=self.now)
        self.emit()
        for offset, day in ((60, "2026-11-08"), (120, "2026-11-06"), (180, "2026-11-08")):
            self.http.day = day
            self.service.refresh(["VST"], now=self.now + offset, force=True)
            notices = self.emit(self.now + offset)
            self.assertEqual(len(notices), 1)
            self.assertIn("date changed", notices[0][1])
        self.assertEqual(self.emit(self.now + 181), [])

    def test_uncertain_cross_source_date_change_is_a_conflict_not_a_revision(self):
        self.http.primary_error = ApiError("unavailable", 403)
        self.service.refresh(["VST"], now=self.now)
        self.emit()
        self.http.primary_error = None
        self.http.day = "2026-11-08"
        self.service.refresh(["VST"], now=self.now + 60, force=True)
        notices = self.emit(self.now + 60)
        self.assertEqual(len(notices), 1)
        self.assertIn("source disagreement", notices[0][1])
        self.assertIn("not a confirmed date revision", notices[0][1])
        self.assertIn("Source conflict", self.service.report(["VST"], now=self.now + 60))
        self.assertEqual(self.emit(self.now + 61), [])

    def test_empty_errors_stale_and_reported_data_never_erase_last_schedule(self):
        self.service.refresh(["VST"], now=self.now)
        self.http.primary_error = ApiError("quota", 429)
        self.http.csv = '{"Information":"limit"}'
        self.service.refresh(["VST"], now=self.now + 60, force=True)
        self.assertEqual(len(self.signals.rows("earnings", ["VST"])), 1)
        self.assertIn("Sources unavailable", self.service.report(["VST"], now=self.now + 60))
        self.assertEqual(self.service.alerts(["VST"], now=self.now + 3 * 86400), [])
        self.http.primary_error = None
        self.http.actual = 0
        self.service.refresh(["VST"], now=self.now + 120, force=True)
        self.assertEqual(self.service.alerts(["VST"], now=self.now + 120), [])

    def test_alpha_calendar_cached_once_and_unknown_time_stays_unknown(self):
        self.http.primary_error = ApiError("forbidden", 403)
        self.http.csv = "symbol,reportDate,fiscalDateEnding\nVST,2026-11-06,2026-09-30\nGOOGL,2026-11-07,2026-09-30\n"
        self.store.add("GOOGL", 2)
        self.service.refresh(["VST", "GOOGL"], now=self.now)
        self.assertEqual(sum(source == "alphavantage" for source, _, _ in self.http.calls), 1)
        self.assertTrue(
            all(
                row["payload"]["hour"] == "unknown"
                for row in self.signals.rows("earnings", ["VST", "GOOGL"])
            )
        )

    def test_new_manual_symbol_expands_cached_alpha_calendar_without_a_second_request(self):
        self.http.primary_error = ApiError("forbidden", 403)
        raw = b"symbol,reportDate,fiscalDateEnding\nVST,2026-11-06,2026-09-30\nGOOGL,2026-11-07,2026-09-30\n"
        fetched = []

        def opener(request, timeout):
            fetched.append(request.full_url)
            return io.BytesIO(raw)

        transport = SignalHTTP(SimpleNamespace(api_key="fixture"), self.store, opener=opener)
        self.http.alpha_csv = transport.alpha_csv
        with patch("trabe.signals.time.time", return_value=self.now):
            self.service.refresh(["VST"], now=self.now)
        self.assertNotIn(
            "GOOGL", self.signals.get("earnings-source", "alpha-calendar")["payload"]["symbols"]
        )
        with patch("trabe.signals.time.time", return_value=self.now + 60):
            self.service.refresh(["GOOGL"], now=self.now + 60)
        self.assertEqual(len(fetched), 1)
        self.assertEqual(self.store.usage(), 1)
        self.assertEqual(len(self.signals.rows("earnings", ["GOOGL"])), 1)
        self.assertIn("2026-11-07", self.service.report(["GOOGL"], now=self.now + 60))
        self.assertEqual(
            self.signals.get("earnings-source", "alpha-calendar")["payload"]["checked_at"], self.now
        )
        self.assertEqual(
            self.signals.rows("earnings", ["GOOGL"])[0]["payload"]["checked_at"], self.now
        )

    def test_held_exposure_and_pause_do_not_require_a_live_quote(self):
        self.store.remove("VST")
        Ledger(self.store).record("VST", "buy", "2.5", "100", "EUR", "2026-11-02")
        self.service.refresh(["VST"], now=self.now)
        notice = self.service.alerts(["VST"], now=self.now)[0]
        self.assertIn("Held: 2.5 shares", notice[1])
        self.assertIn("exposure unavailable", notice[1])
        self.store.pause_automation(self.now + 60)
        self.assertFalse(
            self.signals.notify_once(
                123, "earnings", notice[0], notice[1], now=self.now, setting="earnings_enabled"
            )
        )
        self.assertIn("VST", self.service.report(["VST"], now=self.now))

    def test_cooperative_stop_never_calls_fallback(self):
        self.http.primary_error = WorkStopped("paused")
        with self.assertRaises(WorkStopped):
            self.service.refresh(["VST"], now=self.now)
        self.assertEqual(len(self.http.calls), 1)

    def test_before_open_report_is_not_upcoming_after_the_open(self):
        self.http.day = "2026-11-03"
        self.service.refresh(["VST"], now=self.now)
        self.assertEqual(len(self.service.alerts(["VST"], now=self.now)), 1)
        after_open = datetime(2026, 11, 3, 16, tzinfo=UTC).timestamp()
        self.assertEqual(self.service.alerts(["VST"], now=after_open), [])

    def test_after_close_without_actuals_stays_unconfirmed_and_does_not_send_new_warning(self):
        self.http.day = "2026-11-03"
        self.http.hour = "amc"
        self.service.refresh(["VST"], now=self.now)
        after_close = datetime(2026, 11, 3, 22, tzinfo=UTC).timestamp()
        self.assertIn("release status unconfirmed", self.service.report(["VST"], now=after_close))
        self.assertEqual(self.service.alerts(["VST"], now=after_close), [])
        self.assertFalse(self.signals.rows("earnings", ["VST"])[0]["payload"]["reported"])
        self.http.actual = 0
        self.service.refresh(["VST"], now=after_close, force=True)
        self.assertTrue(self.signals.rows("earnings", ["VST"])[0]["payload"]["reported"])
        self.assertEqual(self.service._upcoming(["VST"], after_close), [])


if __name__ == "__main__":
    unittest.main()
