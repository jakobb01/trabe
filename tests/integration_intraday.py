"""Production runtime with real HTTP/WebSocket frames; no outside network or secrets."""

import base64
import hashlib
import json
import queue
import struct
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import websocket

from trabe.api import Telegram, request_json
from trabe.calendar import NY, Session
from trabe.config import Config
from trabe.monitor import Monitor
from trabe.providers.router import MarketRouter
from trabe.runtime import run
from trabe.store import Store


class FixtureCalendar:
    def __init__(self):
        now = time.time()
        self.session = Session(
            datetime.fromtimestamp(now, NY).date().isoformat(), now - 600, now + 3600
        )

    def at(self, _):
        return self.session

    def previous(self, day):
        return Session((day - timedelta(days=1)).isoformat(), 0, 0)

    def describe(self, _):
        return "fixture regular session"


class IntradayRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.config = Config("100:" + "a" * 32, 123, "alpha", self.path / "agent.db", "fixture-key")
        self.store = Store(self.config.database)
        self.store.add("VST", 2)
        self.messages, self.updates, self.rest = [], [], []
        self.frames = queue.Queue()
        self.connections = self.active = self.max_active = 0
        self.subscriptions = set()
        self.guard = threading.Lock()
        self.calendar = FixtureCalendar()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, data):
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = urlsplit(self.path).path.rsplit("/", 1)[-1]
                if method == "getMe":
                    result = {"username": "fixture_bot"}
                elif method == "getWebhookInfo":
                    result = {"url": ""}
                elif method == "getUpdates":
                    time.sleep(0.02)
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
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    return self.stream()
                fixture.rest.append((self.path, self.headers.get("X-Finnhub-Token")))
                endpoint = urlsplit(self.path)
                ticker = parse_qs(endpoint.query).get("symbol", ["VST"])[0]
                if endpoint.path.endswith("profile2"):
                    result = {"ticker": ticker, "currency": "USD", "exchange": "NYSE"}
                elif endpoint.path.endswith("market-status"):
                    result = {
                        "exchange": "US",
                        "timezone": "America/New_York",
                        "isOpen": True,
                        "session": "regular",
                        "t": time.time(),
                    }
                else:
                    result = {"c": 100, "pc": 100, "o": 100, "dp": 0, "t": time.time()}
                self.respond(result)

            def stream(self):
                digest = hashlib.sha1(
                    (
                        self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                    ).encode()
                ).digest()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", base64.b64encode(digest).decode())
                self.end_headers()
                with fixture.guard:
                    fixture.connections += 1
                    fixture.active += 1
                    fixture.max_active = max(fixture.max_active, fixture.active)
                self.connection.settimeout(0.1)

                def read(count):
                    result = b""
                    while len(result) < count:
                        part = self.connection.recv(count - len(result))
                        if not part:
                            raise EOFError
                        result += part
                    return result

                try:
                    while True:
                        try:
                            head = read(2)
                        except TimeoutError:
                            head = None
                        if head:
                            length = head[1] & 127
                            if length == 126:
                                length = struct.unpack("!H", read(2))[0]
                            if length == 127:
                                length = struct.unpack("!Q", read(8))[0]
                            mask = read(4) if head[1] & 128 else b"\0" * 4
                            data = bytes(
                                value ^ mask[i % 4] for i, value in enumerate(read(length))
                            )
                            if head[0] & 15 == 8:
                                break
                            if head[0] & 15 == 1:
                                command = json.loads(data)
                                with fixture.guard:
                                    if command["type"] == "subscribe":
                                        fixture.subscriptions.add(command["symbol"])
                                    else:
                                        fixture.subscriptions.discard(command["symbol"])
                        try:
                            frame = fixture.frames.get_nowait()
                        except queue.Empty:
                            continue
                        if frame is None:
                            break
                        raw = json.dumps(frame).encode()
                        prefix = (
                            bytes([0x81, len(raw)])
                            if len(raw) < 126
                            else b"\x81\x7e" + struct.pack("!H", len(raw))
                        )
                        self.connection.sendall(prefix + raw)
                except (EOFError, ConnectionError, OSError):
                    pass
                finally:
                    with fixture.guard:
                        fixture.active -= 1
                        fixture.subscriptions.clear()
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.errors = []
        self.stop = threading.Event()
        self.monitor = None

    def transport(self, url, payload=None, timeout=20, headers=None):
        endpoint = urlsplit(url)
        local = f"http://127.0.0.1:{self.server.server_port}{endpoint.path}?{endpoint.query}"
        return request_json(local, payload, timeout, headers=headers)

    def socket_factory(self, _url, **kwargs):
        return websocket.create_connection(
            f"ws://127.0.0.1:{self.server.server_port}/stream", **kwargs
        )

    def start(self):
        market = MarketRouter(self.config, self.store)
        market.finnhub.transport = self.transport
        market.finnhub.calendar = self.calendar
        self.store.set_state("finnhub_verified_key", market.finnhub.key_fingerprint)

        def make_monitor(store, config, finnhub):
            self.monitor = Monitor(
                store, config, finnhub, calendar=self.calendar, socket_factory=self.socket_factory
            )
            return self.monitor

        def worker():
            try:
                with patch("trabe.workers.Monitor", side_effect=make_monitor):
                    run(
                        self.config,
                        telegram=Telegram(self.config.token, self.transport),
                        market=market,
                        stop_event=self.stop,
                        poll_timeout=0,
                    )
            except BaseException as error:
                self.errors.append(error)

        self.stop.clear()
        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def finish(self):
        self.stop.set()
        self.worker.join(10)
        self.assertFalse(self.worker.is_alive(), "runtime failed to stop")
        self.assertEqual(self.errors, [])

    def wait_for(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        self.fail(f"Condition timed out; runtime errors: {self.errors!r}")

    def push(self, text, user=123):
        with self.guard:
            self.updates.append(
                {
                    "update_id": len(self.updates) + 1,
                    "message": {
                        "text": text,
                        "from": {"id": user},
                        "chat": {"id": user, "type": "private"},
                    },
                }
            )

    def text(self, part):
        return any(part in message["text"] for message in self.messages)

    def test_stream_breach_recovery_pause_reconnect_and_restart(self):
        self.start()
        try:
            self.wait_for(lambda: self.text("started"))
            self.push("/provider finnhub")
            self.push("/monitor on")
            self.wait_for(lambda: "VST" in self.subscriptions)
            stamp = int(time.time() * 1000)
            self.frames.put(
                {
                    "type": "trade",
                    "data": [
                        {"s": "VST", "p": 94, "t": stamp},
                        {"s": "VST", "p": 101, "t": stamp + 1},
                    ],
                }
            )
            self.wait_for(lambda: self.text("trabe dip alert"))
            self.assertEqual(len(self.store.recent_alerts()), 1)
            self.push("/remove VST")
            self.push("/add VST")
            self.wait_for(lambda: self.text("Removed VST") and self.text("Saved VST"))
            self.wait_for(lambda: self.monitor.engine.registered("VST"))
            fresh_stamp = int(time.time() * 1000)
            self.frames.put({"type": "trade", "data": [{"s": "VST", "p": 93, "t": fresh_stamp}]})
            self.wait_for(
                lambda: (
                    self.monitor.latest("VST") is not None
                    and self.monitor.latest("VST")["source_time"] >= fresh_stamp / 1000
                )
            )
            self.assertEqual(len(self.store.recent_alerts()), 1)
            self.push("/pause 24h", user=999)
            self.push("/add GOOGL")
            self.wait_for(lambda: "GOOGL" in self.subscriptions)
            self.frames.put(None)
            self.wait_for(lambda: self.connections >= 2 and "VST" in self.subscriptions)
            # A malformed frame closes the old socket before the replacement opens.
            self.frames.put(["malformed frame"])
            self.wait_for(lambda: self.connections >= 3 and "VST" in self.subscriptions)
            self.frames.put(
                {"type": "trade", "data": [{"s": "VST", "p": 90, "t": int(time.time() * 1000)}]}
            )
            self.push("/pause 24h")
            self.wait_for(lambda: self.text("Automation paused until"))
            self.wait_for(lambda: self.active == 0)
            before = len(self.rest)
            for command in ("/status", "/alerts", "/history VST 5", "/storage"):
                self.push(command)
            self.wait_for(lambda: self.text("trabe storage"))
            self.assertEqual(len(self.rest), before)
            self.assertEqual(self.max_active, 1)
            self.assertEqual(len(self.store.recent_alerts()), 1)
            self.assertTrue(all(key == "fixture-key" for _, key in self.rest))
            self.assertTrue(all(message["chat_id"] == 123 for message in self.messages))
        finally:
            self.finish()
        count = len(self.rest)
        self.start()
        try:
            self.push("/status")
            self.wait_for(lambda: self.store.state("last_poll") != "0")
            self.assertTrue(self.store.automation_paused())
            self.assertEqual(len(self.rest), count)
            self.push("/resume")
            self.wait_for(lambda: "VST" in self.subscriptions)
            self.frames.put(
                {"type": "trade", "data": [{"s": "VST", "p": 89, "t": int(time.time() * 1000)}]}
            )
            self.wait_for(lambda: self.monitor.latest("VST") is not None)
            self.assertEqual(len(self.store.recent_alerts()), 1)
        finally:
            self.finish()


if __name__ == "__main__":
    unittest.main()
