"""Provider pacing and errors must not turn a valid stock into a false lookup failure."""

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from trabe.api import ApiError, MarketData
from trabe.scans import Scans
from trabe.store import Store

VST_QUOTE = {
    "Global Quote": {
        "01. symbol": "VST",
        "05. price": "138.3700",
        "10. change percent": "-1.8304%",
        "07. latest trading day": "2026-10-30",
    }
}
RATE_NOTICE = {
    "Information": "Please consider spreading out your free API requests "
    "more sparingly (1 request per second). Free key rate limit (25 requests per day)."
}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "state.db")

    def test_observed_vistra_response_parses_as_vst(self):
        quote, cached = MarketData("key", self.store, lambda *a, **k: VST_QUOTE).quote("VST")
        self.assertEqual(quote["ticker"], "VST")
        self.assertEqual(quote["price"], 138.37)
        self.assertFalse(cached)

    def test_requests_are_spaced_across_separate_clients_and_cache_is_free(self):
        now = [1000000.0]
        arrivals, finishes, delays = [], [], []

        def transport(url, **kwargs):
            ticker = parse_qs(urlsplit(url).query)["symbol"][0]
            arrivals.append(now[0])
            now[0] += 0.25
            finishes.append(now[0])
            return {"Global Quote": VST_QUOTE["Global Quote"] | {"01. symbol": ticker}}

        def sleep(delay):
            delays.append(delay)
            now[0] += delay

        with (
            patch("trabe.api.time.time", side_effect=lambda: now[0]),
            patch("trabe.api.time.sleep", side_effect=sleep),
        ):
            MarketData("key", self.store, transport).quote("GOOGL")
            other = MarketData("key", Store(self.store.path), transport)
            other.quote("VST")
            self.assertTrue(other.quote("VST")[1])
        self.assertEqual(len(arrivals), 2)
        self.assertGreaterEqual(arrivals[1] - finishes[0], 1.099)
        self.assertEqual(len(delays), 1)
        self.assertEqual(self.store.usage(), 2)

    def test_http_200_rate_notice_is_a_limit_not_a_missing_stock(self):
        market = MarketData("key", self.store, lambda *a, **k: RATE_NOTICE)
        with self.assertRaises(ApiError) as caught:
            market.quote("VST")
        self.assertEqual(caught.exception.code, 429)
        self.assertIn("provider limit", str(caught.exception))
        self.assertNotIn("invalid", str(caught.exception))
        self.assertGreater(float(self.store.state("market_retry_after")), time.time())
        with self.assertRaisesRegex(ApiError, "cooling down"):
            MarketData("key", Store(self.store.path)).quote("GOOGL")
        self.assertEqual(self.store.usage(), 1)

    def test_empty_provider_result_does_not_claim_stock_does_not_exist(self):
        market = MarketData("key", self.store, lambda *a, **k: {"Global Quote": {}})
        with self.assertRaises(ApiError) as caught:
            market.quote("VST")
        self.assertIn("no quote for VST", str(caught.exception))
        self.assertIn("does not confirm", str(caught.exception))

    def test_scan_shows_request_limit_and_stops_spending_quota(self):
        self.store.add("VST", 1)
        self.store.add("GOOGL", 2)
        scans = Scans(self.store, MarketData("key", self.store, lambda *a, **k: RATE_NOTICE))
        self.addCleanup(scans.close)
        scans.start()
        scans._thread.join(timeout=3)
        summary = self.store.latest_scan()[2]
        self.assertIn("provider request limit reached", summary)
        self.assertIn("provider limit", summary)
        self.assertNotIn("VST: data unavailable", summary)
        self.assertEqual(self.store.usage(), 1)

    def test_two_clients_share_one_cache_fill_for_the_same_symbol(self):
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def transport(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(3)
            return VST_QUOTE

        first = MarketData("key", self.store, transport)
        second = MarketData("key", Store(self.store.path), transport)

        def second_quote():
            second_started.set()
            return second.quote("VST")

        with ThreadPoolExecutor(max_workers=2) as workers:
            a = workers.submit(first.quote, "VST")
            self.assertTrue(entered.wait(1))
            b = workers.submit(second_quote)
            self.assertTrue(second_started.wait(1))
            time.sleep(0.05)
            self.assertEqual(len(calls), 1)
            release.set()
            self.assertFalse(a.result(timeout=3)[1])
            self.assertTrue(b.result(timeout=3)[1])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
