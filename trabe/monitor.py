"""One outbound stream, bounded ingestion, minute storage and prompt alert checks."""

import fcntl
import json
import logging
import math
import os
import queue
import threading
import time
from urllib.parse import urlencode

from trabe.alerts import Alerts, breach
from trabe.api import ApiError
from trabe.calendar import USCalendar
from trabe.outbox import Outbox

logger = logging.getLogger(__name__)


class SubscriptionDenied(Exception):
    """An explicit provider denial, distinct from an invalid ticker or outage."""


class Ingestion:
    def __init__(self, store, owner_id: int, calendar=None):
        self.store = store
        self.calendar = calendar or USCalendar()
        self.alerts = Alerts(store, owner_id)
        self.lock = threading.RLock()
        self.prices, self.buckets, self.registrations = {}, {}, {}
        self.claimed = set()
        self.rules = {}
        self.settings = store.settings()
        self.allowed = set()
        self.paused = False
        self.received = self.rejected = 0

    def configure(self, settings: dict, rules: dict, stocks: list[str], *, paused=False):
        with self.lock:
            self.settings, self.rules, self.allowed = settings, rules, set(stocks)
            self.paused = paused
            self.prices = {
                ticker: value for ticker, value in self.prices.items() if ticker in self.allowed
            }
            self.registrations = {
                ticker: value
                for ticker, value in self.registrations.items()
                if ticker in self.allowed
            }

    def register(self, ticker: str, session, baseline: dict, now: float):
        with self.lock:
            instrument, feed = self.store.join_session(session, ticker, now)
            self.claimed = {entry for entry in self.claimed if entry[1] == session.day}
            self.registrations[ticker] = (session, instrument, feed, baseline)
            with self.store.connection() as db:
                if db.execute(
                    "SELECT 1 FROM alert_events WHERE ticker=? AND rule='previous-close' AND session_date=?",
                    (ticker, session.day),
                ).fetchone():
                    self.claimed.add((ticker, session.day))

    def latest(self, ticker: str):
        with self.lock:
            value = self.prices.get(ticker)
            return dict(value) if value else None

    def last_source(self):
        with self.lock:
            return max((q["source_time"] for q in self.prices.values()), default=0)

    def registered(self, ticker):
        with self.lock:
            return ticker in self.registrations

    def observe(self, trade: dict, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            self.received += 1
            if (
                self.paused
                or self.settings["monitoring"] != "on"
                or self.settings["market_provider"] != "finnhub"
            ):
                self.rejected += 1
                return False
            ticker, price, source_ms = trade.get("s"), trade.get("p"), trade.get("t")
            if ticker not in self.allowed or ticker not in self.registrations:
                self.rejected += 1
                return False
            if (
                type(price) not in (int, float)
                or type(source_ms) is not int
                or not math.isfinite(price)
                or price <= 0
            ):
                self.rejected += 1
                return False
            source = source_ms / 1000
            session, instrument, feed, baseline = self.registrations[ticker]
            if (
                not session.contains(source)
                or not session.contains(now)
                or not -2 <= now - source <= int(self.settings["max_source_age_seconds"])
            ):
                self.rejected += 1
                return False
            previous = self.prices.get(ticker) or self.store.cached_live("finnhub", ticker)
            if previous and (
                source < previous["source_time"]
                or (source == previous["source_time"] and price == previous["price"])
            ):
                self.rejected += 1
                return False
            minute_ms = source_ms // 60000 * 60000
            key = (ticker, session.day, minute_ms)
            if key not in self.buckets:
                bucket = self.store.stored_bucket(instrument, feed, session.day, minute_ms)
                if bucket is None:
                    bucket = {
                        "instrument_id": instrument,
                        "feed_id": feed,
                        "session_date": session.day,
                        "minute_start_ms": minute_ms,
                        "observed_open": price,
                        "observed_high": price,
                        "observed_low": price,
                        "observed_last": price,
                        "first_source_ms": source_ms,
                        "last_source_ms": source_ms,
                        "tick_count": 0,
                        "quality_flags": 0,
                    }
                self.buckets[key] = bucket
            bucket = self.buckets[key]
            bucket.update(
                observed_high=max(price, bucket["observed_high"]),
                observed_low=min(price, bucket["observed_low"]),
                observed_last=price,
                last_source_ms=source_ms,
                tick_count=bucket["tick_count"] + 1,
            )
            prior = baseline["previous_close"]
            quote = {
                "ticker": ticker,
                "provider": "finnhub",
                "feed": "us-trades",
                "price_kind": "last-trade",
                "currency": "USD",
                "price": float(price),
                "previous_close": prior,
                "change_percent": (price / prior - 1) * 100,
                "source_time": source,
                "received_at": now,
                "trading_day": session.day,
                "baseline_valid": bool(baseline["accepted"]),
                "baseline_reason": baseline["reason"],
            }
            self.prices[ticker] = quote
            enabled, threshold = self.rules.get(
                ticker, (True, float(self.settings["dip_threshold_percent"]))
            )
            if (
                enabled
                and (ticker, session.day) not in self.claimed
                and breach(price, prior, threshold) is not None
            ):
                if self.alerts.evaluate(ticker, quote, bucket, baseline, now=now):
                    self.claimed.add((ticker, session.day))
            return True

    def flush(self, *, now: float | None = None, all_buckets: bool = False):
        now = time.time() if now is None else now
        with self.lock:
            keys = [
                key
                for key, bucket in self.buckets.items()
                if all_buckets or bucket["minute_start_ms"] + 60000 <= now * 1000
            ]
            if not keys:
                return
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for key in keys:
                    self.store.save_bucket(self.buckets[key], db)
                    quote = self.prices.get(key[0])
                    if quote:
                        self.store.save_live(quote, db)
            for key in keys:
                del self.buckets[key]

    def gap(self, *, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for day in {
                value[0].day for value in self.registrations.values() if value[0].contains(now)
            }:
                self.store.mark_session_gap(day)


class Monitor:
    def __init__(self, store, config, finnhub, *, calendar=None, socket_factory=None):
        self.store, self.config, self.finnhub = store, config, finnhub
        self.calendar = calendar or USCalendar()
        self.engine = Ingestion(store, config.owner_id, self.calendar)
        self.socket_factory = socket_factory
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.validation = threading.Event()
        self.validation_generation = 0
        self.pending = queue.Queue(maxsize=2048)
        self.drain_lock = threading.Lock()
        self.socket = None
        self.worker = self.consumer = None
        self.last_status = 0.0
        self.fault_until = 0.0

    def latest(self, ticker):
        return self.engine.latest(ticker)

    def request_validation(self):
        self.validation_generation += 1
        self.validation.set()
        self.wake.set()
        self.store.set_state("provider_validation", "queued")

    def cancel_validation(self):
        self.validation_generation += 1
        self.validation.clear()

    def changed(self):
        self.controls()
        self.wake.set()

    def start(self):
        self.worker = threading.Thread(target=self.run, daemon=True, name="finnhub-stream")
        self.consumer = threading.Thread(target=self.consume, daemon=True, name="price-ingestion")
        self.consumer.start()
        self.worker.start()

    def controls(self):
        with self.store.connection() as db:
            settings = dict(db.execute("SELECT key,value FROM settings"))
            states = dict(
                db.execute(
                    "SELECT key,value FROM state WHERE key IN ('pause_until','storage_paused')"
                )
            )
            from trabe.ledger import Ledger

            watched = [
                row[0] for row in db.execute("SELECT ticker FROM watchlist ORDER BY tier,ticker")
            ]
            held = Ledger._held(db)
            stocks = watched + sorted(held - set(watched))
            rules = {
                ticker: (
                    bool(enabled),
                    threshold
                    if threshold is not None
                    else float(settings["dip_threshold_percent"]),
                )
                for ticker, enabled, threshold in db.execute(
                    "SELECT ticker,enabled,threshold FROM alert_preferences"
                )
            }
        # Held-only symbols need prices but do not implicitly enable dip alerts.
        for ticker in held - set(watched):
            rules[ticker] = (False, float(settings["dip_threshold_percent"]))
        self.engine.configure(
            settings,
            rules,
            stocks,
            paused=float(states.get("pause_until", "0")) > time.time()
            or bool(states.get("storage_paused")),
        )
        return settings, states, stocks

    def allowed(self, settings, states, now):
        if settings["market_provider"] != "finnhub" or settings["monitoring"] != "on":
            return "monitoring off"
        if float(states.get("pause_until", "0")) > now:
            return "paused by owner"
        if states.get("storage_paused"):
            return states["storage_paused"]
        if not self.finnhub.verified():
            return "Finnhub credential validation required; use /provider finnhub"
        if self.fault_until > now:
            return "ingestion recovering; retry scheduled"
        return ""

    def status(self, state, *, force=False, **values):
        if not force and time.time() - self.last_status < 5:
            return
        self.last_status = time.time()
        payload = {
            "state": state,
            "updated_at": self.last_status,
            "received": self.engine.received,
            "rejected": self.engine.rejected,
            "queued": self.pending.qsize(),
            "last_source": self.engine.last_source(),
            "subscribed": [],
            "unsupported": {},
            **values,
        }
        self.store.set_state("monitor_status", json.dumps(payload))

    def consume(self):
        while not self.stop.is_set() or not self.pending.empty():
            try:
                with self.drain_lock:
                    try:
                        trade = self.pending.get_nowait()
                    except queue.Empty:
                        trade = None
                    if trade is not None:
                        self.engine.observe(trade)
                    self.engine.flush()
                if trade is None:
                    self.stop.wait(0.1)
            except Exception:
                logger.error("Price ingestion failed; retrying without logging provider details.")
                self.fault_until = time.time() + 30
                self.stop.wait(1)
        self.engine.flush(all_buckets=True)

    def enqueue(self, trade):
        try:
            self.pending.put_nowait(trade)
        except queue.Full:
            # Drain in source order before consuming this frame; never drop a brief breach.
            with self.drain_lock:
                self.engine.gap()
                while not self.pending.empty():
                    self.engine.observe(self.pending.get_nowait())
                self.engine.observe(trade)

    def validate_requested(self):
        if not self.validation.is_set():
            return
        self.validation.clear()
        generation = self.validation_generation
        self.store.set_state("provider_validation", "checking free-account access")
        try:
            stocks = [ticker for ticker, _ in self.store.watchlist()][:2]
            self.finnhub.validate(stocks or None)
            if generation != self.validation_generation:
                return
            self.store.select_provider("finnhub")
            self.store.set_state("provider_validation", "verified")
            Outbox(self.store).send(
                self.config.owner_id,
                "Finnhub access verified and selected. Use /monitor on to enable intraday collection.",
            )
        except Exception:
            self.store.set_state(
                "provider_validation",
                "failed; inspect configured key/plan and retry /provider finnhub",
            )
            Outbox(self.store).send(
                self.config.owner_id,
                "Finnhub validation failed. The current provider is unchanged; check key/plan access and retry /provider finnhub.",
            )

    def run(self):
        fd = os.open(str(self.store.path) + ".stream.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.status("another local stream owns this database", force=True)
                return
            delay = 2
            while not self.stop.is_set():
                self.wake.clear()
                try:
                    self.validate_requested()
                    settings, states, stocks = self.controls()
                    reason = self.allowed(settings, states, time.time())
                    session = None
                    if not reason:
                        try:
                            session = self.calendar.at(time.time())
                        except ValueError:
                            reason = "exchange calendar unavailable/expired; monitoring suspended"
                    if not reason and (not session or not session.contains(time.time())):
                        reason = self.calendar.describe(time.time())
                    if not reason and not stocks:
                        reason = "watchlist empty"
                    if not reason and len(stocks) > 50:
                        reason = (
                            "free stream limit is 50 symbols; reduce watchlist before monitoring"
                        )
                    if reason:
                        self.status(reason)
                        self.wake.wait(5)
                        continue
                    with self.store.connection() as db:
                        unfinished = db.execute(
                            "SELECT 1 FROM market_sessions WHERE finalized=0 AND closes+600<=?",
                            (time.time(),),
                        ).fetchone()
                    if unfinished:
                        self.status("waiting for prior-session rollup")
                        self.wake.wait(1)
                        continue
                    self.stream(session, stocks)
                    delay = 2
                except SubscriptionDenied:
                    self.disconnect()
                    self.status(
                        "Finnhub subscription denied; check plan/symbol entitlement", force=True
                    )
                    self.engine.gap()
                    self.wake.wait(60)
                except Exception:
                    self.disconnect()
                    self.status("stream/provider unavailable; reconnect scheduled", force=True)
                    self.engine.gap()
                    self.wake.wait(delay)
                    delay = min(60, delay * 2)
        finally:
            self.disconnect()
            os.close(fd)

    def stream(self, session, stocks):
        import websocket

        logging.getLogger("websocket").disabled = True
        subscribed = set()
        unsupported = {}
        attempted = {}
        gap_limit = self.store.settings()["baseline_max_gap_percent"]

        def prepare(ticker):
            settings, states, _ = self.controls()
            if self.stop.is_set() or self.allowed(settings, states, time.time()):
                return False
            attempted[ticker] = time.time()
            try:
                baseline = self.finnhub.prepare_baseline(ticker, session, automatic=True)
                self.engine.register(ticker, session, baseline, time.time())
                if not baseline["accepted"]:
                    unsupported[ticker] = baseline["reason"]
                else:
                    unsupported.pop(ticker, None)
                return True
            except ApiError as error:
                if error.code == 429:
                    unsupported[ticker] = "Finnhub rate limited; retry after cooldown"
                elif error.code in (401, 403):
                    unsupported[ticker] = "Finnhub access denied; check key/plan entitlement"
                else:
                    unsupported[ticker] = "US listing/quote unavailable; retry scheduled"
                return False
            except ValueError as error:
                unsupported[ticker] = str(error)
                return False
            except Exception:
                unsupported[ticker] = "quote/profile access unavailable; retry scheduled"
                return False

        valid = [ticker for ticker in stocks if prepare(ticker)]
        if not valid:
            self.status("no streamable symbols", force=True, unsupported=unsupported)
            self.wake.wait(10)
            return
        factory = self.socket_factory or websocket.create_connection
        self.socket = factory(
            "wss://ws.finnhub.io?" + urlencode({"token": self.finnhub.api_key}), timeout=10
        )
        self.socket.settimeout(1)
        for ticker in valid:
            self.socket.send(json.dumps({"type": "subscribe", "symbol": ticker}))
            subscribed.add(ticker)
        self.status(
            "connected",
            force=True,
            subscribed=sorted(subscribed),
            unsupported=unsupported,
            session=session.day,
        )
        checked = last_frame = time.time()
        try:
            while not self.stop.is_set():
                now = time.time()
                if now - checked >= 2 or self.wake.is_set():
                    checked = now
                    self.wake.clear()
                    settings, states, wanted = self.controls()
                    if (
                        self.allowed(settings, states, now)
                        or not session.contains(now)
                        or len(wanted) > 50
                        or self.validation.is_set()
                        or settings["baseline_max_gap_percent"] != gap_limit
                    ):
                        break
                    for ticker in subscribed - set(wanted):
                        self.socket.send(json.dumps({"type": "unsubscribe", "symbol": ticker}))
                        subscribed.remove(ticker)
                        attempted.pop(ticker, None)
                    for ticker in wanted:
                        missing = not self.engine.registered(ticker)
                        needs_baseline = (
                            ticker not in subscribed or ticker in unsupported or missing
                        )
                        if (
                            needs_baseline
                            and (
                                now - attempted.get(ticker, 0) >= 60
                                or (ticker in subscribed and missing)
                            )
                            and prepare(ticker)
                        ):
                            if ticker not in subscribed:
                                self.socket.send(
                                    json.dumps({"type": "subscribe", "symbol": ticker})
                                )
                                subscribed.add(ticker)
                    if now - last_frame > 60:
                        raise TimeoutError
                    self.status(
                        "connected",
                        subscribed=sorted(subscribed),
                        unsupported=unsupported,
                        session=session.day,
                        last_frame=last_frame,
                        last_source=self.engine.last_source(),
                    )
                try:
                    raw = self.socket.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw or len(raw) > 1_048_576:
                    raise ValueError("Invalid stream frame size")
                last_frame = time.time()
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("Invalid stream frame")
                if payload.get("type") == "error":
                    raise SubscriptionDenied
                if payload.get("type") == "trade":
                    trades = payload.get("data")
                    if not isinstance(trades, list) or len(trades) > 10000:
                        raise ValueError("Invalid trade batch")
                    for trade in trades:
                        if isinstance(trade, dict) and trade.get("s") in subscribed:
                            self.enqueue(trade)
        finally:
            self.disconnect()
            self.engine.gap()
            with self.drain_lock:
                while not self.pending.empty():
                    self.engine.observe(self.pending.get_nowait())
                self.engine.flush(all_buckets=True)

    def disconnect(self):
        socket, self.socket = self.socket, None
        if socket is not None:
            try:
                socket.close(timeout=1)
            except Exception:
                pass

    def close(self):
        self.stop.set()
        self.wake.set()
        self.disconnect()
        if self.worker:
            self.worker.join(timeout=20)
        if self.consumer:
            self.consumer.join(timeout=5)
