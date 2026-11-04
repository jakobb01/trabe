"""One offline HTTP walkthrough of signals, Telegram delivery and restart.

Opt-in: exercises the new transport/coordinator boundary without real credentials
or another poller for the owner's bot.
"""

import json
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from trabe.api import MarketData, Telegram, request_json
from trabe.config import Config
from trabe.insights import Insights
from trabe.runtime import run
from trabe.signals import SignalHTTP
from trabe.store import Store


class SignalsRuntimeTests(unittest.TestCase):
    def test_commands_background_delivery_pause_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                "100:" + "a" * 32, 123, "alpha-fixture", Path(directory) / "app.db", "fh-fixture"
            )
            store = Store(config.database)
            store.add("VST", 2)
            messages, updates, calls, errors = [], [], [], []
            guard, stop = threading.Lock(), threading.Event()
            news_started, news_release = threading.Event(), threading.Event()
            self.addCleanup(news_release.set)
            report_day = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()

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
                        with guard:
                            result = [u for u in updates if u["update_id"] >= payload["offset"]]
                    elif method == "sendMessage":
                        with guard:
                            messages.append(payload)
                        result = {"message_id": len(messages)}
                    else:
                        result = True
                    self.respond({"ok": True, "result": result})

                def do_GET(self):
                    path = urlsplit(self.path).path
                    with guard:
                        calls.append(path)
                    if path.endswith("company-news"):
                        news_started.set()
                        news_release.wait(10)
                        result = [
                            {
                                "id": 42,
                                "related": "VST",
                                "headline": "Fixture raises guidance",
                                "summary": "Revenue rose. The company raised its forecast.",
                                "datetime": int(time.time()) - 60,
                                "url": "https://example.com/fixture-news",
                                "source": "Fixture",
                            }
                        ]
                    elif path.endswith("calendar/earnings"):
                        result = {
                            "earningsCalendar": [
                                {
                                    "symbol": "VST",
                                    "date": report_day,
                                    "year": 2026,
                                    "quarter": 3,
                                    "hour": "bmo",
                                }
                            ]
                        }
                    else:
                        result = {"data": [], "meta": {}}
                    self.respond(result)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            def local_url(url):
                parsed = urlsplit(url)
                local = f"http://127.0.0.1:{server.server_port}{parsed.path}"
                return local + ("?" + parsed.query if parsed.query else "")

            def transport(url, payload=None, timeout=20):
                return request_json(local_url(url), payload, timeout)

            def opener(request, timeout):
                return urlopen(
                    Request(local_url(request.full_url), headers=dict(request.header_items())),
                    timeout=timeout,
                )

            def insights_factory(config, store, box, monitor):
                return Insights(
                    config,
                    store,
                    box,
                    monitor,
                    http_factory=lambda *a, **kw: SignalHTTP(*a, **kw, opener=opener),
                )

            def push(update_id, text, user=123):
                with guard:
                    updates.append(
                        {
                            "update_id": update_id,
                            "message": {
                                "text": text,
                                "from": {"id": user},
                                "chat": {"id": user, "type": "private"},
                            },
                        }
                    )

            def wait_for(predicate):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    with guard:
                        if predicate():
                            return
                    time.sleep(0.02)
                self.fail(f"Runtime walkthrough timed out; errors: {errors!r}")

            def contains(text):
                return any(text in message["text"] for message in messages)

            def runtime():
                try:
                    run(
                        config,
                        telegram=Telegram(config.token, transport),
                        market=MarketData(config.api_key, store, transport),
                        stop_event=stop,
                        poll_timeout=0,
                    )
                except BaseException as error:
                    errors.append(error)

            with patch("trabe.workers.Insights", side_effect=insights_factory):
                worker = threading.Thread(target=runtime, daemon=True)
                worker.start()
                try:
                    wait_for(lambda: contains("started"))
                    push(1, "/earnings on", user=999)
                    push(2, "/news VST")
                    self.assertTrue(news_started.wait(5), "background news request never started")
                    push(3, "/status")
                    wait_for(lambda: contains("Uptime:"))
                    self.assertEqual(store.settings()["earnings_enabled"], "off")
                    news_release.set()
                    wait_for(lambda: contains("Fixture raises guidance"))
                    push(4, "/briefing now")
                    wait_for(lambda: contains("local sentiment is heuristic"))
                    joined = "\n".join(message["text"] for message in messages)
                    for section in (
                        "Positions",
                        "Watchlist prices",
                        "Earnings",
                        "Insider purchases",
                    ):
                        self.assertIn(section, joined)
                    push(5, "/earnings on")
                    wait_for(lambda: contains("📅 Upcoming earnings"))
                    push(6, "/pause 1h")
                    wait_for(lambda: contains("Automation paused until"))
                    before = len(messages)
                    count = len(calls)
                    push(7, "/news VST")
                    wait_for(
                        lambda: any(
                            "Fixture raises guidance" in m["text"] for m in messages[before:]
                        )
                    )
                    self.assertEqual(len(calls), count)
                    push(8, "/resume")
                    wait_for(lambda: store.state("telegram_offset") == "9")
                finally:
                    news_release.set()
                    stop.set()
                    worker.join(8)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(all(message["chat_id"] == 123 for message in messages))
                count = len(calls)
                before = len(messages)
                stop.clear()
                worker = threading.Thread(target=runtime, daemon=True)
                worker.start()
                try:
                    push(9, "/status")
                    wait_for(lambda: any("Uptime:" in m["text"] for m in messages[before:]))
                finally:
                    stop.set()
                    worker.join(8)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(
                    len(calls), count, "fresh persisted sources should survive restart"
                )
                self.assertEqual(sum("📅 Upcoming earnings" in m["text"] for m in messages), 1)
                with store.connection() as db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM signal_notifications").fetchone()[0], 1
                    )
                self.assertEqual(store.state("heartbeat"), "0")


if __name__ == "__main__":
    unittest.main()
