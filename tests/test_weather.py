"""Standalone weather using an in-memory MET response and a temporary service database."""

import gzip
import io
import json
import tempfile
import threading
import unittest
import zlib
from datetime import UTC, datetime
from email.utils import formatdate
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from trabe import weather_met as met
from trabe.outbox import Outbox
from trabe.store import Store
from trabe.weather import CACHE_LIMIT, SLOT_WINDOW, Weather, cached, preferences, update_preferences
from trabe.weather_ui import apply_action, contact_input, coordinates_input

OWNER = 123
CONTACT = "ops.weather+fixture@trabe-fixture.org"
COORDINATES = "46.0500,14.5000"
OTHER = "44.0000,-14.1234"


def epoch(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()


def fixture(now, sparse=False):
    issue = now - 1800

    def iso(stamp):
        return datetime.fromtimestamp(stamp, UTC).isoformat()

    periods = {}
    for hour in range(25):
        if sparse and hour not in (0, 6, 18):
            continue
        data = {"instant": {"details": {"air_temperature": 4 + hour / 2, "wind_speed": 4}}}
        if sparse:
            data["next_6_hours"] = {"details": {"precipitation_amount": 8 if hour == 6 else 0}}
        else:
            data["next_1_hours"] = {"details": {"precipitation_amount": 2 if hour == 3 else 0}}
            data["next_6_hours"] = {"details": {"precipitation_amount": 20}}
        periods[hour] = {"time": iso(now + hour * met.HOUR), "data": data}
    return {
        "properties": {"meta": {"updated_at": iso(issue)}, "timeseries": list(periods.values())}
    }


class HttpFixture(io.BytesIO):
    def __init__(self, status, body=b"", **headers):
        super().__init__(body)
        self.code, self.headers = status, headers


class MetTests(unittest.TestCase):
    def setUp(self):
        self.now = epoch("2026-11-20T08:00")
        self.modified = formatdate(self.now - 3600, usegmt=True)
        self.body = json.dumps(fixture(self.now)).encode()

    def request(self, *responses, modified=""):
        with patch("trabe.weather_met.build_opener") as opener:
            opener.return_value.open.side_effect = responses
            response = met.fetch(COORDINATES, CONTACT, modified, self.now)
            return response, [call.args[0] for call in opener.return_value.open.call_args_list]

    def test_compression_and_identifiable_conditional_https(self):
        raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        for compression, body in (
            ("gzip", gzip.compress(self.body)),
            ("deflate", zlib.compress(self.body)),
            ("deflate", raw.compress(self.body) + raw.flush()),
        ):
            with self.subTest(compression=compression):
                response, requests = self.request(
                    HttpFixture(301, Location=met.ENDPOINT + "?lon=14.5000&lat=46.0500"),
                    HttpFixture(
                        200,
                        body,
                        **{"Content-Encoding": compression, "Last-Modified": self.modified},
                    ),
                    modified=self.modified,
                )
                self.assertEqual(response.forecast["issue"], self.now - 1800)
                self.assertEqual(response.modified, self.modified)
                self.assertEqual(len(requests), 2)
                for request in requests:
                    self.assertEqual(request.get_method(), "GET")
                    self.assertIn(CONTACT, request.get_header("User-agent"))
                    self.assertEqual(request.get_header("If-modified-since"), self.modified)
                    self.assertEqual(request.get_header("Accept-encoding"), "gzip, deflate")

    def test_redirect_rejects_disclosing_contact_or_altered_coordinates(self):
        for destination in (
            "http://api.met.no/weatherapi/locationforecast/2.0/compact?lat=46.0500&lon=14.5000",
            "https://api.met.no.evil.org/weatherapi/locationforecast/2.0/compact?lat=46.0500&lon=14.5000",
            met.ENDPOINT + "?lat=1.0000&lon=14.5000",
        ):
            response, requests = self.request(HttpFixture(302, Location=destination))
            self.assertEqual(response.status, 0)
            self.assertEqual(len(requests), 1)

    def test_bounded_body_future_validator_and_deprecation(self):
        oversized = gzip.compress(b" " * (met.MAX_BODY + 1))
        response, _ = self.request(HttpFixture(200, oversized, **{"Content-Encoding": "gzip"}))
        self.assertIsNone(response.forecast)
        response, _ = self.request(
            HttpFixture(200, self.body, **{"Last-Modified": formatdate(self.now + 1, usegmt=True)})
        )
        self.assertEqual(response.modified, "")
        response, _ = self.request(HttpFixture(203, self.body))
        self.assertEqual(response.status, 203)
        self.assertIsNone(response.forecast)

    def test_retry_after_seconds_and_http_date(self):
        for value in ("17200", formatdate(self.now + 17200, usegmt=True)):
            response, _ = self.request(HttpFixture(429, **{"Retry-After": value}))
            self.assertEqual(response.retry_at, self.now + 17200)

    def test_precipitation_coverage_and_wet_window(self):
        zone = ZoneInfo("Europe/Ljubljana")
        forecast = met.parse_forecast(fixture(self.now), self.now)
        card = met.summarize(forecast, "Home", zone, self.now)
        self.assertIn("2 mm across 24 h", card)
        self.assertIn("12:00 CET → Fri 20 Nov 13:00 CET", card)
        self.assertNotIn("Partial", card)
        forecast = met.parse_forecast(fixture(self.now, sparse=True), self.now)
        card = met.summarize(forecast, "", zone, self.now)
        self.assertIn("8 mm across 18 h", card)
        self.assertIn("(6 h estimate)", card)
        self.assertIn("Partial forecast coverage", card)
        older = met.parse_forecast(fixture(self.now + 13 * met.HOUR), self.now + 13 * met.HOUR)
        older["issue"] = self.now - 13 * met.HOUR
        self.assertIn("older forecast", met.summarize(older, "", zone, self.now, cached=True))
        older["issue"] = self.now - met.MAX_ISSUE_AGE
        self.assertIsNone(met.summarize(older, "", zone, self.now))


class WeatherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "weather.db"
        self.store = Store(self.path)
        self.now = epoch("2026-11-20T08:00")
        self.fetch = Mock(side_effect=self.success)
        self.telegram = Mock()
        self.restart()

    def restart(self, owner=OWNER):
        self.store = Store(self.path)
        self.outbox = Outbox(self.store, owner_id=owner)
        self.worker = Weather(
            self.store,
            self.outbox,
            owner,
            fetch=self.fetch,
            clock=lambda: self.now,
            jitter=lambda: 30,
        )

    def success(self, coordinates, contact, modified, now):
        return met.Response(
            200,
            formatdate(now - 1800, usegmt=True),
            now + 2 * met.HOUR,
            forecast=met.parse_forecast(fixture(now), now),
        )

    def setup_weather(self, **values):
        settings = (
            dict(
                coordinates=COORDINATES,
                timezone="UTC",
                contact=CONTACT,
                accepted_by=OWNER,
                terms=met.TERMS_REVISION,
            )
            | values
        )
        self.change(self.now - 86400, **settings)

    def change(self, now, **values):
        with self.store.connection() as db:
            update_preferences(db, now, **values)

    def preview(self):
        with self.store.connection() as db:
            generation = preferences(db)["generation"]
            return apply_action(db, OWNER, f"{generation}:preview", self.now)

    def cache(self, coordinates=COORDINATES):
        with self.store.connection() as db:
            return cached(db, coordinates)

    def flush(self):
        self.outbox.flush(self.telegram, now=self.now)

    def test_install_off_human_terms_contact_and_idle_read(self):
        with self.store.connection() as db:
            self.assertEqual(preferences(db)["enabled"], 0)
            self.assertEqual(preferences(db)["timezone"], self.store.settings(db)["timezone"])
        connection = self.store.connection
        statements = []

        from contextlib import contextmanager

        @contextmanager
        def traced():
            with connection() as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(self.store, "connection", side_effect=traced):
            self.worker.run_once()
        self.assertFalse(any(statement.startswith("BEGIN") for statement in statements))
        self.change(self.now, coordinates=COORDINATES)
        self.assertIn("accept", self.preview()[1])
        self.worker.run_once()
        self.change(self.now, accepted_by=OWNER, terms=met.TERMS_REVISION)
        self.assertIn("contact", self.preview()[1])
        self.worker.run_once()
        self.fetch.assert_not_called()

    def test_strict_contact_atoms_and_coordinate_truncation(self):
        self.assertEqual(contact_input(CONTACT), CONTACT)
        self.assertEqual(
            contact_input("https://trabe-fixture.org/contact"), "https://trabe-fixture.org/contact"
        )
        for local in (".", ".ops", "ops.", "ops..weather", "x" * 65):
            with self.subTest(local=local), self.assertRaises(ValueError):
                contact_input(f"{local}@trabe-fixture.org")
        limit = "a" * 64 + "@trabe-fixture.org"
        self.assertEqual(contact_input(limit), limit)
        self.assertEqual(coordinates_input({"text": "-46.12349 0.00009"}), "-46.1234,0.0000")

    def test_cache_304_preserves_issue_exact_validator_and_restart_gates(self):
        self.setup_weather()
        original_time = self.now
        self.fetch.side_effect = None
        first = self.success(COORDINATES, CONTACT, "", self.now)
        expires = self.now + 4 * met.HOUR
        self.fetch.side_effect = [
            met.Response(200, first.modified, expires, forecast=first.forecast),
            met.Response(304, formatdate(self.now, usegmt=True)),
        ]
        self.preview()
        self.worker.run_once()
        self.flush()
        self.restart()
        self.now = expires + 29
        self.assertEqual(self.preview(), ("preview", ""))
        self.worker.run_once()
        self.assertEqual(self.fetch.call_count, 1)
        self.now += 1
        self.preview()
        self.worker.run_once()
        self.flush()
        self.fetch.assert_called_with(COORDINATES, CONTACT, first.modified, self.now)
        cache = self.cache()
        self.assertEqual(
            (cache.modified, cache.expires, cache.fetched_at),
            (first.modified, expires, original_time),
        )
        self.assertEqual(cache.forecast()["issue"], first.forecast["issue"])
        self.assertIn("304", self.telegram.send.call_args.args[1])

    def test_429_survives_restart_and_other_coordinate(self):
        self.setup_weather()
        retry = self.now + 5 * met.HOUR
        self.fetch.side_effect = [met.Response(429, retry_at=retry), met.Response(403)]
        self.preview()
        self.worker.run_once()
        self.flush()
        self.assertIn("429", self.telegram.send.call_args.args[1])
        self.change(self.now, coordinates=OTHER)
        self.restart()
        self.now = retry - 1
        self.preview()
        self.worker.run_once()
        self.assertEqual(self.fetch.call_count, 1)
        self.now += 1
        self.preview()
        self.worker.run_once()
        self.flush()
        self.assertIn("403", self.telegram.send.call_args.args[1])

    def test_deprecation_preserves_old_data_and_shows_status(self):
        self.setup_weather()
        self.preview()
        self.worker.run_once()
        issue = self.cache().forecast()["issue"]
        self.flush()
        self.now = self.cache().next_at
        self.fetch.side_effect = None
        self.fetch.return_value = met.Response(203, forecast={"issue": self.now})
        self.preview()
        self.worker.run_once()
        self.flush()
        self.assertEqual(self.cache().forecast()["issue"], issue)
        self.assertIn("deprecated (203)", self.telegram.send.call_args.args[1])
        self.assertIn("saved data", self.telegram.send.call_args.args[1])

    def test_capacity_never_evicts_an_active_gate(self):
        self.setup_weather()
        for latitude in range(CACHE_LIMIT + 1):
            self.change(self.now, coordinates=f"{latitude:.4f},14.5000")
            self.preview()
            self.worker.run_once()
            self.flush()
        self.assertEqual(self.fetch.call_count, CACHE_LIMIT)
        self.assertEqual(self.cache("0.0000,14.5000").next_at, self.now + met.REQUEST_INTERVAL + 30)
        self.now += met.REQUEST_INTERVAL + 30
        self.preview()
        self.worker.run_once()
        self.assertEqual(self.fetch.call_count, CACHE_LIMIT + 1)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM weather_cache").fetchone()[0], CACHE_LIMIT
            )

    def test_location_change_mid_fetch_keeps_gates_and_drops_stale_result(self):
        self.setup_weather(enabled=1, times="08:00")
        self.preview()

        def move_and_fetch(*args):
            thread = threading.Thread(
                target=self.change, args=(self.now,), kwargs={"coordinates": OTHER}
            )
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive(), "MET request held the Store lock")
            return self.success(*args)

        self.fetch.side_effect = move_and_fetch
        self.worker.run_once()
        self.assertEqual(self.outbox.pending(), 0)
        self.restart()
        self.change(self.now, coordinates=COORDINATES)
        self.assertGreater(self.cache().next_at, self.now)
        self.assertEqual(self.preview(), ("preview", ""))
        self.worker.run_once()
        self.assertEqual(self.fetch.call_count, 1)

    def test_latest_catchup_restart_and_same_day_cap_across_edits(self):
        self.setup_weather(enabled=1, times="08:00 08:05 08:10 08:20")
        self.now += 9 * 60
        self.worker.run_once()
        self.flush()
        self.restart()
        self.worker.run_once()
        self.assertEqual(self.outbox.pending(), 0)
        for minute in (10, 20):
            self.now = epoch(f"2026-11-20T08:{minute}")
            self.worker.run_once()
            self.flush()
        self.change(self.now + 1, times="08:05 08:10 08:30 08:40")
        self.now = epoch("2026-11-20T08:30")
        self.worker.run_once()
        self.flush()
        self.now = epoch("2026-11-20T08:40")
        self.worker.run_once()
        self.flush()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.telegram.send.call_count, 4)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT hhmm FROM weather_slots ORDER BY hhmm").fetchall(),
                [("08:05",), ("08:10",), ("08:20",), ("08:30",)],
            )
        for delivery in self.telegram.send.call_args_list:
            self.assertIn("Issued Fri 20 Nov 07:39 UTC", delivery.args[1])
            self.assertEqual(
                delivery.kwargs["reply_markup"]["inline_keyboard"][0], met.attribution()
            )

    def test_dst_gap_and_fold(self):
        self.now = epoch("2026-03-29T01:35")
        self.setup_weather(enabled=1, times="02:30", timezone="Europe/Ljubljana", days=64)
        self.worker.run_once()
        self.fetch.assert_not_called()
        self.now = epoch("2026-10-25T00:30")
        self.worker.run_once()
        self.flush()
        self.restart()
        self.now += met.HOUR
        self.worker.run_once()
        self.flush()
        self.assertEqual(self.telegram.send.call_count, 1)

    def test_late_or_superseded_or_disabled_delivery_is_discarded(self):
        self.setup_weather(enabled=1, times="08:00 08:05 08:45 09:30")
        self.worker.run_once()
        self.now += 5 * 60
        self.flush()
        self.telegram.send.assert_not_called()
        self.worker.run_once()
        self.now += SLOT_WINDOW
        self.flush()
        self.telegram.send.assert_not_called()
        self.now = epoch("2026-11-20T08:45")
        self.worker.run_once()
        self.assertEqual(self.outbox.pending(), 1)
        self.change(self.now, enabled=0)
        self.flush()
        self.telegram.send.assert_not_called()

    def test_outbox_refuses_queued_weather_for_pre_pair_chat(self):
        self.setup_weather(enabled=1, times="08:00")
        self.worker.run_once()
        self.assertEqual(self.outbox.pending(), 1)
        self.restart(owner=456)
        self.flush()
        self.telegram.send.assert_not_called()
        self.assertEqual(self.outbox.pending(), 0)

    def test_storage_and_global_pause_guard_both_work_and_delivery(self):
        self.setup_weather(enabled=1, times="08:00 08:10 08:20")
        self.store.set_state("storage_paused", "fixture guard")
        self.worker.run_once()
        self.fetch.assert_not_called()
        self.store.set_state("storage_paused", "")
        self.now += 10 * 60
        self.worker.run_once()
        self.store.set_state("storage_paused", "fixture guard")
        self.flush()
        self.telegram.send.assert_not_called()
        self.store.set_state("storage_paused", "")
        self.store.set_state("pause_until", str(self.now + 15 * 60))
        self.now += 10 * 60
        self.worker.run_once()
        self.flush()
        self.telegram.send.assert_not_called()
        self.assertEqual(self.fetch.call_count, 1)
        with self.store.connection() as db:
            self.assertIn("Global automation is paused", preferences(db)["delivery_status"])
