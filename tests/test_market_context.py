"""Session-qualified premarket references, VIX identity and shared public limits."""

import copy
import io
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit

from trabe.api import ApiError
from trabe.calendar import USCalendar
from trabe.config import Config
from trabe.insights import Insights
from trabe.market_context import MarketContext, baseline, parse_chart, target_session
from trabe.outbox import Outbox
from trabe.signals import SignalHTTP, SignalStore, WorkStopped
from trabe.store import Store


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def chart(symbol="VST", *, pre=True):
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "currency": "USD",
                        "instrumentType": "INDEX"
                        if symbol == "^VIX"
                        else "ETF"
                        if symbol in ("SPY", "QQQ")
                        else "EQUITY",
                        "exchangeTimezoneName": "America/New_York",
                        "dataGranularity": "1m",
                        "regularMarketTime": stamp("2026-11-06T21:00:00+00:00"),
                        "regularMarketPrice": 124.8,
                        "previousClose": 112.89,
                        "chartPreviousClose": 138.37,
                        "hasPrePostMarketData": True,
                        "currentTradingPeriod": {
                            "regular": {"start": stamp("2026-11-06T14:30:00+00:00")}
                        },
                    },
                    "timestamp": [
                        stamp("2026-11-06T13:00:00+00:00"),
                        stamp("2026-11-09T12:59:00+00:00"),
                    ],
                    "indicators": {"quote": [{"close": [118.9, 125 if pre else None]}]},
                }
            ],
        }
    }


class MarketContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "agent.db")
        self.store.add("VST", 2)
        self.now = stamp("2026-11-09T13:00:00+00:00")
        self.calendar = USCalendar()

        class HTTP:
            def __init__(self):
                self.calls = []

            def json(self, url, provider):
                self.calls.append((url, provider))
                symbol = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                return copy.deepcopy(chart(symbol))

        self.http = HTTP()
        self.context = MarketContext(self.store, self.http)
        self.repo = SignalStore(self.store)
        self.config = Config("100:" + "a" * 32, 123, "", self.store.path)

    def test_premarket_uses_latest_bar_and_previous_session_close_not_chart_start(self):
        self.context.refresh(["VST"], now=self.now)
        value = self.repo.get("market-context", "VST")["payload"]
        self.assertEqual(value["premarket_price"], "125")
        close, day = baseline(value, target_session(self.calendar, self.now), self.calendar)
        self.assertEqual(str(close), "124.8")
        self.assertEqual(day, "2026-11-06")
        text = "\n".join(self.context.briefing(["VST"], now=self.now))
        self.assertIn("VST: 125.00 USD • +0.16% vs 2026-11-06 close 124.80", text)
        self.assertIn("09 Nov 13:59 CET", text)
        self.assertIn("equity delay unverified", text)
        self.assertEqual(len(self.http.calls), 4)

    def test_regular_session_uses_metadata_previous_close_for_its_actual_day(self):
        raw = chart()
        raw["chart"]["result"][0]["meta"].update(
            regularMarketTime=stamp("2026-11-09T15:00:00+00:00"),
            regularMarketPrice=130,
            previousClose=124.8,
            currentTradingPeriod={"regular": {"start": stamp("2026-11-09T14:30:00+00:00")}},
        )
        now = stamp("2026-11-09T15:00:00+00:00")
        value = parse_chart(raw, "VST", now, self.calendar)
        close, day = baseline(value, target_session(self.calendar, now), self.calendar)
        self.assertEqual(str(close), "124.8")
        self.assertEqual(day, "2026-11-06")

    def test_premarket_prefers_timestamped_prior_close_when_session_metadata_rolls(self):
        raw = chart()
        raw["chart"]["result"][0]["meta"]["currentTradingPeriod"] = {
            "regular": {"start": stamp("2026-11-09T14:30:00+00:00")}
        }
        value = parse_chart(raw, "VST", self.now, self.calendar)
        close, day = baseline(value, target_session(self.calendar, self.now), self.calendar)
        self.assertEqual(str(close), "124.8")
        self.assertEqual(day, "2026-11-06")

    def test_weekend_and_early_briefing_do_not_relabel_friday_as_current_premarket(self):
        for now in (stamp("2026-11-08T12:00:00+00:00"), stamp("2026-11-09T07:00:00+00:00")):
            raw = chart(pre=False)
            raw["chart"]["result"][0]["timestamp"] = [stamp("2026-11-06T13:00:00+00:00")]
            raw["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [118.9]
            self.http.json = lambda *a, **kw: copy.deepcopy(raw)
            self.context.refresh(["VST"], now=now, force=True, include_market=False)
            text = "\n".join(self.context.briefing(["VST"], now=now))
            self.assertIn("Premarket not started", text)
            self.assertIn(
                "VST: premarket unavailable • previous close 124.80 USD (2026-11-06)", text
            )

    def test_stale_bar_and_vix_delay_units_baseline_are_explicit(self):
        self.context.refresh(["VST"], now=self.now)
        text = "\n".join(self.context.briefing(["VST"], now=self.now + 1800))
        self.assertIn("stale", text)
        self.assertIn("VIX • volatility index", text)
        self.assertIn("points", text)
        self.assertIn("15m delayed • prior session", text)
        self.assertIn("vs 2026-11-05 close", text)
        self.assertNotIn("VIX: 124.80 USD", text)

    def test_mismatched_index_currency_future_data_and_bad_bars_are_rejected(self):
        for field, value in (
            ("symbol", "OTHER"),
            ("currency", "EUR"),
            ("regularMarketPrice", True),
            ("regularMarketPrice", "NaN"),
            ("regularMarketTime", self.now + 60),
            ("instrumentType", "FUTURE"),
        ):
            raw = chart()
            raw["chart"]["result"][0]["meta"][field] = value
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                parse_chart(raw, "VST", self.now, self.calendar)
        raw = chart("^VIX")
        raw["chart"]["result"][0]["meta"]["instrumentType"] = "ETF"
        with self.assertRaises(ValueError):
            parse_chart(raw, "^VIX", self.now, self.calendar)
        raw = chart()
        raw["chart"]["result"][0]["timestamp"].append(self.now)
        with self.assertRaises(ValueError):
            parse_chart(raw, "VST", self.now, self.calendar)

    def test_cache_restart_and_outage_preserve_observations_without_hiding_failure(self):
        self.context.refresh(["VST"], now=self.now)
        self.context.refresh(["VST"], now=self.now + 1)
        self.assertEqual(len(self.http.calls), 4)

        def offline(*args, **kwargs):
            raise OSError("private connection detail")

        self.http.json = offline
        restarted = MarketContext(Store(self.store.path), self.http)
        restarted.refresh(["VST"], now=self.now + 2, force=True)
        text = "\n".join(restarted.briefing(["VST"], now=self.now + 2))
        self.assertIn("125.00 USD", text)
        self.assertIn("Some market requests failed/deferred", text)
        self.assertNotIn("private connection", text)
        self.assertEqual(len(self.repo.rows("market-context")), 4)

    def test_cancellation_is_not_recorded_as_source_failure(self):
        def stop(*args, **kwargs):
            raise WorkStopped("paused")

        self.http.json = stop
        with self.assertRaises(WorkStopped):
            self.context.refresh(["VST"], now=self.now)
        self.assertTrue(all(not s["error"] for s in self.repo.status("market-context")))

    def test_future_cache_is_excluded_from_an_earlier_briefing(self):
        self.context.refresh(["VST"], now=self.now)
        text = "\n".join(self.context.briefing(["VST"], now=self.now - 60))
        self.assertIn("VST: premarket unavailable; no saved observation", text)
        self.assertIn("VIX: unavailable", text)

    def test_manual_briefing_refreshes_benchmarks_once_and_render_is_cached(self):
        from test_signals import FixtureHTTP

        from trabe.ledger import Ledger

        class HTTP(FixtureHTTP):
            calls = []

            def json(self, url, provider=None):
                if provider == "yahoo-chart":
                    self.calls.append(unquote(urlsplit(url).path.rsplit("/", 1)[-1]))
                    return chart(self.calls[-1])
                return super().json(url, provider)

        self.store.add("SPY", 2)
        Ledger(self.store).record("VST", "buy", "1", "100", "USD", "2026-11-03")
        controls = Insights(self.config, self.store, Outbox(self.store), http_factory=HTTP)
        with patch("trabe.insights.time.time", return_value=self.now):
            controls.handle("briefing", ["now"])
            controls.process_job()
            controls.process_job()
        self.assertEqual(HTTP.calls.count("QQQ"), 1)
        self.assertEqual(HTTP.calls.count("SPY"), 1)
        self.assertEqual(HTTP.calls.count("^VIX"), 1)
        self.assertEqual(HTTP.calls.count("VST"), 1)
        count = len(HTTP.calls)
        text = controls.render_briefing(["VST"], self.now)
        self.assertIn("Premarket", text)
        self.assertEqual(len(HTTP.calls), count)

    def test_chart_cap_leaves_room_for_news_and_counts_against_shared_limit(self):
        calls = []
        http = SignalHTTP(
            self.config, self.store, opener=lambda *a, **kw: calls.append(1) or io.BytesIO(b"{}")
        )
        now = time.time()
        with self.store.connection() as db:
            db.executemany("INSERT INTO signal_http_calls VALUES ('yahoo-chart',?)", [(now,)] * 60)
        with self.assertRaises(ApiError):
            http.json(
                "https://query1.finance.yahoo.com/v8/finance/chart/SPY", provider="yahoo-chart"
            )
        self.assertEqual(calls, [])
        http.xml("https://feeds.finance.yahoo.com/rss/2.0/headline", provider="yahoo")
        self.assertEqual(calls, [1])
        with self.store.connection() as db:
            db.executemany("INSERT INTO signal_http_calls VALUES ('yahoo',?)", [(now,)] * 59)
        with self.assertRaises(ApiError):
            http.xml("https://feeds.finance.yahoo.com/rss/2.0/headline", provider="yahoo")
        self.assertEqual(calls, [1])

    def test_chart_429_cools_down_shared_yahoo_and_unapproved_hosts_are_rejected(self):
        def limited(request, timeout):
            raise HTTPError(request.full_url, 429, "limited", {"Retry-After": "120"}, io.BytesIO())

        http = SignalHTTP(self.config, self.store, opener=limited)
        with self.assertRaises(ApiError):
            http.json(
                "https://query1.finance.yahoo.com/v8/finance/chart/SPY", provider="yahoo-chart"
            )
        self.assertGreater(float(self.store.state("yahoo_retry_after")), time.time())
        with self.assertRaises(ApiError):
            http.xml("https://feeds.finance.yahoo.com/rss/2.0/headline", provider="yahoo")
        with self.assertRaises(ValueError):
            http.json("https://example.com/chart", provider="yahoo-chart")


if __name__ == "__main__":
    unittest.main()
