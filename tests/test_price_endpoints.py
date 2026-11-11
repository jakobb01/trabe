"""Exact minute identity, first-observation persistence and bounded shared claims."""

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

from trabe.calendar import USCalendar
from trabe.price_endpoints import EndpointStore, parse_endpoint
from trabe.store import Store


def chart(symbol, session, *, opening="100", closing="102", edges=("open", "close")):
    times = [session.opens if e == "open" else session.closes - 60 for e in edges]
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "symbol": symbol.replace(".", "-"),
                        "currency": "USD",
                        "instrumentType": "ETF" if symbol == "SPY" else "EQUITY",
                        "dataGranularity": "1m",
                        "exchangeTimezoneName": "America/New_York",
                    },
                    "timestamp": times,
                    "indicators": {
                        "quote": [{"open": [opening] * len(times), "close": [closing] * len(times)}]
                    },
                }
            ],
        }
    }


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.calendar = USCalendar()
        self.session = self.calendar.session(date(2026, 11, 10))

    def parse(self, raw=None, symbol="VST", edge="open", received=None):
        return parse_endpoint(
            chart(symbol, self.session) if raw is None else raw,
            symbol=symbol,
            session=self.session,
            edge=edge,
            received_at=self.session.closes + 1800 if received is None else received,
        )

    def test_A01_equity_etf_and_explicit_alias_identity(self):
        for symbol in ("VST", "SPY", "BRK.B"):
            for edge, price in (("open", "100"), ("close", "102")):
                value = self.parse(symbol=symbol, edge=edge)
                self.assertEqual(value["symbol"], symbol)
                self.assertEqual(value["provider_symbol"], symbol.replace(".", "-"))
                self.assertEqual(value["price"], price)
                self.assertEqual(value["received_at"], self.session.closes + 1800)
                self.assertIn("unverified", value["quality"])

    def test_A01_verified_source_fixtures_from_2026_11_11(self):
        for symbol in ("vst", "spy"):
            raw = json.loads(
                (
                    Path(__file__).parent / "fixtures" / f"endpoint_{symbol}_20261111.json"
                ).read_text()
            )
            for edge in ("open", "close"):
                value = parse_endpoint(
                    raw,
                    symbol=symbol.upper(),
                    session=self.session,
                    edge=edge,
                    received_at=raw["observed_at"],
                )
                self.assertEqual(value["edge"], edge)

    def test_A12_concurrent_workers_claim_once(self):
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "db")
            store.set_setting("advisor_results_enabled", "on")

            def claim(_):
                return EndpointStore(store).reserve(
                    "VST", self.session.day, "advisor_results", self.session.opens + 300
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sum(pool.map(claim, range(2))), 1)

    def test_A02_identity_and_malformed_arrays_fail_closed(self):
        for key, value in (
            ("symbol", "OTHER"),
            ("currency", "EUR"),
            ("instrumentType", "INDEX"),
            ("dataGranularity", "1d"),
            ("exchangeTimezoneName", "UTC"),
        ):
            raw = chart("VST", self.session)
            raw["chart"]["result"][0]["meta"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.parse(raw)
        for mode in ("multiple", "empty", "error", "length", "oversized", "none", "quote"):
            raw = chart("VST", self.session)
            row = raw["chart"]["result"][0]
            if mode == "multiple":
                raw["chart"]["result"].append(copy.deepcopy(row))
            elif mode == "empty":
                raw["chart"]["result"] = []
            elif mode == "error":
                raw["chart"]["error"] = {"code": "missing"}
            elif mode == "length":
                row["indicators"]["quote"][0]["open"] = []
            elif mode == "oversized":
                row["timestamp"] = [self.session.opens] * 10001
            elif mode == "none":
                row["timestamp"] = None
            else:
                row["indicators"]["quote"] = []
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.parse(raw)

    def test_A03_invalid_prices_and_duplicate_or_future_bars(self):
        for price in (None, 0, -1, "NaN", "Infinity", True, "abc"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                self.parse(chart("VST", self.session, opening=price))
        raw = chart("VST", self.session)
        raw["chart"]["result"][0]["timestamp"] = [self.session.opens] * 2
        with self.assertRaises(ValueError):
            self.parse(raw)
        raw["chart"]["result"][0]["timestamp"] = [self.session.opens, self.session.closes + 1900]
        with self.assertRaises(ValueError):
            self.parse(raw)

    def test_A04_no_adjacent_or_daily_fallback(self):
        for offset in (-60, 60, -3600, 7 * 86400):
            raw = chart("VST", self.session, edges=("open",))
            row = raw["chart"]["result"][0]
            row["timestamp"][0] += offset
            row["meta"].update(regularMarketPrice=100, previousClose=100)
            with self.assertRaises(ValueError):
                self.parse(raw)

    def test_A05_readiness_boundaries(self):
        for edge, ready in (
            ("open", self.session.opens + 300),
            ("close", self.session.closes + 1800),
        ):
            raw = chart("VST", self.session, edges=(edge,))
            with self.assertRaises(ValueError):
                self.parse(raw, edge=edge, received=ready - 1)
            self.parse(raw, edge=edge, received=ready)

    def test_A06_calendar_bounds_are_verified(self):
        from trabe.calendar import Session

        self.session = self.calendar.session(date(2026, 11, 27))
        self.assertEqual(self.session.minutes, 210)
        self.parse(edge="close")
        self.session = Session(self.session.day, self.session.opens + 60, self.session.closes)
        with self.assertRaises(ValueError):
            self.parse()

    def test_A12_A13_first_observation_immutability_and_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "db")
            store.set_setting("advisor_results_enabled", "on")
            endpoints = EndpointStore(store)
            value = self.parse()
            now = value["received_at"]
            self.assertTrue(endpoints.reserve("VST", self.session.day, "advisor_results", now))
            self.assertFalse(
                EndpointStore(Store(store.path)).reserve(
                    "VST", self.session.day, "advisor_results", now
                )
            )
            with store.connection() as db:
                endpoints.save(value, db=db)
                self.assertEqual(endpoints.save(value | {"price": "999"}, db=db), value)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE market_price_endpoints SET payload='{}'")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "INSERT OR REPLACE INTO market_price_endpoints SELECT * FROM market_price_endpoints"
                    )
            self.assertEqual(endpoints.get("VST", self.session.day, "open"), value)
            with store.connection() as db:
                db.execute("DROP TRIGGER immutable_price_endpoint")
                db.execute("UPDATE market_price_endpoints SET sha256='bad'")
            with self.assertRaisesRegex(ValueError, "integrity"):
                endpoints.get("VST", self.session.day, "open")

    def test_A11_combined_hourly_sublimit_and_provider_cooldown(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "db")
            store.set_setting("advisor_results_enabled", "on")
            repo = EndpointStore(store)
            now = self.session.closes + 1800
            for index in range(12):
                self.assertTrue(repo.reserve(f"S{index}", self.session.day, "advisor_results", now))
            self.assertFalse(repo.reserve("LAST", self.session.day, "advisor_results", now))
            store.set_state("yahoo_retry_after", str(now + 7200))
            self.assertFalse(repo.reserve("LAST", self.session.day, "advisor_results", now + 3601))

    def test_A10_stop_is_not_a_source_failure(self):
        from trabe.signals import WorkStopped

        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "db")
            store.set_setting("advisor_results_enabled", "on")
            http = Mock()
            http.json.side_effect = WorkStopped("cancelled")
            with self.assertRaises(WorkStopped):
                EndpointStore(store).collect(
                    http,
                    "VST",
                    self.session,
                    ["open"],
                    feature="advisor_results",
                    now=self.session.opens + 300,
                    clock=lambda: self.session.opens + 300,
                )
            with store.connection() as db:
                self.assertEqual(
                    db.execute("SELECT last_error FROM market_endpoint_attempts").fetchone()[0], ""
                )
