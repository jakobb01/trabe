"""Real HTTP + SQLite runtime checks, run inside an isolated test container.

No real Telegram token, market key, or internet access is used. A loopback server
speaks the actual request/response contracts to the production HTTPS client.
"""

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from trabe.api import MarketData, Telegram, request_json
from trabe.config import Config
from trabe.runtime import run
from trabe.store import Store


class RuntimeHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.messages, self.updates, self.market_calls = [], [], []
        self.market_arrivals = []
        self.guard = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, value):
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = self.path.rsplit("/", 1)[-1]
                if method == "getMe":
                    result = {"id": 100, "username": "fixture_bot"}
                elif method == "getWebhookInfo":
                    result = {"url": ""}
                elif method == "getUpdates":
                    time.sleep(0.03)
                    with fixture.guard:
                        result = [u for u in fixture.updates if u["update_id"] >= payload["offset"]]
                elif method == "sendMessage":
                    with fixture.guard:
                        fixture.messages.append(payload)
                    result = {"message_id": len(fixture.messages)}
                else:
                    result = True
                self.respond({"ok": True, "result": result})

            def do_GET(self):
                with fixture.guard:
                    fixture.market_calls.append(self.path)
                    now = time.monotonic()
                    too_fast = bool(
                        fixture.market_arrivals and now - fixture.market_arrivals[-1] < 1
                    )
                    fixture.market_arrivals.append(now)
                if too_fast:
                    self.respond({"Information": "Free API rate limit: 1 request per second."})
                    return
                ticker = parse_qs(urlsplit(self.path).query)["symbol"][0]
                self.respond(
                    {
                        "Global Quote": {
                            "01. symbol": ticker,
                            "05. price": "100.25",
                            "10. change percent": "-5.5%",
                            "07. latest trading day": "2026-10-30",
                        }
                    }
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.stop = threading.Event()
        self.errors = []

    def transport(self, url, payload=None, timeout=20):
        endpoint = urlsplit(url)
        local = f"http://127.0.0.1:{self.server.server_port}{endpoint.path}"
        if endpoint.query:
            local += "?" + endpoint.query
        return request_json(local, payload, timeout)

    def start_runtime(self):
        config = Config("100:" + "a" * 32, 123, "fixture-key", self.path / "agent.db")
        market = MarketData(config.api_key, Store(config.database), self.transport)
        telegram = Telegram(config.token, self.transport)
        self.stop.clear()

        def worker():
            try:
                run(config, telegram=telegram, market=market, stop_event=self.stop, poll_timeout=0)
            except BaseException as error:
                self.errors.append(error)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def stop_runtime(self):
        self.stop.set()
        self.worker.join(timeout=5)
        self.assertFalse(self.worker.is_alive(), "runtime did not shut down cleanly")
        self.assertEqual(self.errors, [])

    def push(self, update_id, text, user=123):
        with self.guard:
            self.updates.append(
                {
                    "update_id": update_id,
                    "message": {
                        "text": text,
                        "from": {"id": user},
                        "chat": {"id": user, "type": "private"},
                    },
                }
            )

    def wait_text(self, text):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with self.guard:
                if any(text in message["text"] for message in self.messages):
                    return
            time.sleep(0.02)
        self.fail(f"No Telegram reply containing {text!r}; worker errors: {self.errors!r}")

    def test_commands_scan_and_restart_over_real_http(self):
        self.start_runtime()
        try:
            self.wait_text("started")
            self.push(1, "/add HACK", user=999)
            self.push(2, "/add IBM 1")
            self.push(3, "/set quote_cache_minutes 120")
            self.push(4, "/quote IBM")
            self.push(5, "/scan")
            self.push(6, "Status")
            self.wait_text("Watchlist scan #1")
            self.wait_text("Uptime:")
        finally:
            self.stop_runtime()
        store = Store(self.path / "agent.db")
        self.assertEqual(store.watchlist(), [("IBM", 1)])
        self.assertEqual(store.settings()["quote_cache_minutes"], "120")
        self.assertEqual(len(self.market_calls), 1)
        self.assertTrue(all(message["chat_id"] == 123 for message in self.messages))
        self.assertTrue(any("reply_markup" in message for message in self.messages))
        count = len(self.market_calls)
        self.start_runtime()
        try:
            self.push(7, "/quote IBM")
            self.push(8, "/settings")
            self.wait_text("quote_cache_minutes = 120")
            self.wait_text("Alpha Vantage, cached")
        finally:
            self.stop_runtime()
        self.assertEqual(len(self.market_calls), count)
        self.assertEqual(store.state("telegram_offset"), "9")
        self.assertEqual(store.state("heartbeat"), "0")

    def test_two_fresh_symbols_succeed_with_a_one_request_per_second_provider(self):
        store = Store(self.path / "paced.db")
        first = MarketData("fixture-key", store, self.transport)
        second = MarketData("fixture-key", Store(store.path), self.transport)
        google, _ = first.quote("GOOGL")
        vistra, _ = second.quote("VST")
        self.assertEqual(google["ticker"], "GOOGL")
        self.assertEqual(vistra["ticker"], "VST")
        self.assertEqual(len(self.market_calls), 2)
        self.assertGreaterEqual(self.market_arrivals[1] - self.market_arrivals[0], 1)


if __name__ == "__main__":
    unittest.main()
