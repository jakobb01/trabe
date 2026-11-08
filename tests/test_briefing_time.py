"""Local session display, calendar limits and once-per-session briefing delivery."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from trabe.calendar import USCalendar
from trabe.config import Config
from trabe.insights import Insights
from trabe.outbox import Outbox
from trabe.signals import SignalStore
from trabe.store import Store


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


class BriefingTimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "agent.db")
        self.store.add("VST", 2)
        self.store.set_setting("briefing_enabled", "on")
        self.box = Outbox(self.store)
        self.repo = SignalStore(self.store)
        config = Config("100:" + "a" * 32, 123, "", self.store.path)
        self.insights = Insights(config, self.store, self.box)
        for feature in ("earnings", "insiders", "news"):
            self.repo.ok(feature, "fixture", "VST", stamp("2026-01-02T00:00:00+00:00"))

    def test_local_open_tracks_both_dst_transitions_and_mismatch_weeks(self):
        calendar = USCalendar()
        for day, expected in (
            ("2026-03-06", "15:30 CET"),
            ("2026-03-09", "14:30 CET"),
            ("2026-03-30", "15:30 CEST"),
            ("2026-10-23", "15:30 CEST"),
            ("2026-10-26", "14:30 CET"),
            ("2026-11-02", "15:30 CET"),
        ):
            with self.subTest(day=day):
                text = calendar.describe(stamp(day + "T06:00:00+00:00"), "Europe/Ljubljana")
                self.assertIn(f"opens {day} {expected}", text)
                self.assertIn("(in ", text)

    def test_holiday_weekend_and_after_close_show_next_verified_local_open(self):
        calendar = USCalendar()
        for when, expected in (
            ("2026-11-08T10:00:00+00:00", "2026-11-09 15:30 CET"),
            ("2026-11-26T12:00:00+00:00", "2026-11-27 15:30 CET"),
            ("2026-11-04T22:00:00+00:00", "2026-11-05 15:30 CET"),
        ):
            with self.subTest(when=when):
                text = calendar.describe(stamp(when), "Europe/Ljubljana")
                self.assertIn("closed", text)
                self.assertIn("next opens " + expected, text)

    def test_active_early_close_session_shows_local_open_and_close(self):
        text = USCalendar().describe(stamp("2026-11-27T15:00:00+00:00"), "Europe/Ljubljana")
        self.assertIn("regular session", text)
        self.assertIn("opened 2026-11-27 15:30 CET", text)
        self.assertIn("closes 2026-11-27 19:00 CET", text)

    def test_briefing_rereads_timezone_and_calendar_expiry_stays_visible(self):
        now = stamp("2026-11-04T12:00:00+00:00")
        self.store.set_setting("timezone", "America/Los_Angeles")
        text = self.insights.render_briefing(["VST"], now)
        self.assertIn("(America/Los_Angeles)", text)
        self.assertIn("opens 2026-11-04 06:30 PST", text)
        self.store.set_setting("timezone", "Europe/Ljubljana")
        text = self.insights.render_briefing(["VST"], now)
        self.assertIn("opens 2026-11-04 15:30 CET", text)
        expired = stamp("2029-01-02T12:00:00+00:00")
        text = self.insights.render_briefing(["VST"], expired)
        self.assertIn("US market: calendar unavailable/expired", text)
        self.insights.scheduled_briefing(["VST"], expired)
        self.assertEqual(self.box.pending(), 0)
        self.assertIn("calendar expired", self.store.state("insights_error"))

    def test_local_midnight_and_weekend_date_use_exchange_session_identity(self):
        # Friday's US open is on Saturday in Auckland, including the early close.
        self.store.set_setting("timezone", "Pacific/Auckland")
        self.store.set_setting("briefing_time", "03:00")
        before = stamp("2026-11-27T14:00:00+00:00")
        self.insights.scheduled_briefing(["VST"], before - 1)
        self.assertEqual(self.box.pending(), 0)
        self.insights.scheduled_briefing(["VST"], before)
        self.assertGreater(self.box.pending(), 0)
        self.assertTrue(self.repo.notified("briefing", "daily:2026-11-27"))
        self.assertFalse(self.repo.notified("briefing", "daily:2026-11-28"))
        with self.store.connection() as db:
            text = db.execute("SELECT text FROM outbox ORDER BY id LIMIT 1").fetchone()[0]
        self.assertIn("opens 2026-11-28 03:30 NZDT", text)
        self.assertIn("closes 2026-11-28 07:00 NZDT", text)

    def test_travel_and_restart_cannot_send_same_session_twice(self):
        now = stamp("2026-11-03T14:00:00+00:00")
        self.insights.scheduled_briefing(["VST"], now)
        count = self.box.pending()
        self.assertGreater(count, 0)
        self.store.set_setting("timezone", "Pacific/Auckland")
        self.store.set_setting("briefing_time", "03:00")
        restarted = Insights(self.insights.config, Store(self.store.path), self.box)
        restarted.scheduled_briefing(["VST"], now + 60)
        self.assertEqual(self.box.pending(), count)

    def test_after_open_schedule_and_pause_do_not_send(self):
        self.store.set_setting("timezone", "America/Los_Angeles")
        self.store.set_setting("briefing_time", "08:00")
        # Configured 08:00 is later than the 06:30 US open in this timezone.
        for when in ("2026-11-04T14:00:00+00:00", "2026-11-04T17:00:00+00:00"):
            self.insights.scheduled_briefing(["VST"], stamp(when))
        self.assertEqual(self.box.pending(), 0)
        self.store.set_setting("briefing_time", "06:00")
        now = stamp("2026-11-04T14:00:00+00:00")
        self.store.pause_automation(now + 60)
        self.insights.scheduled_briefing(["VST"], now)
        self.assertEqual(self.box.pending(), 0)

    def test_timezone_change_during_render_cannot_enqueue_old_schedule(self):
        def render(*args):
            self.store.set_setting("timezone", "Pacific/Auckland")
            return "stale timezone briefing"

        with patch.object(self.insights, "render_briefing", side_effect=render):
            self.insights.scheduled_briefing(["VST"], stamp("2026-11-04T12:00:00+00:00"))
        self.assertEqual(self.box.pending(), 0)
        self.assertFalse(self.repo.notified("briefing", "daily:2026-11-04"))

    def test_default_calendar_description_remains_compatible(self):
        now = stamp("2026-11-04T12:00:00+00:00")
        self.assertEqual(USCalendar().describe(now), "opens 2026-11-04T14:30:00+00:00")


if __name__ == "__main__":
    unittest.main()
