"""Shared, bounded event storage and budgeted outbound fundamentals requests."""

import fcntl
import json
import math
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from trabe.api import ApiError

SIGNAL_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS signals(
        kind TEXT NOT NULL,event_key TEXT NOT NULL,symbol TEXT NOT NULL,source TEXT NOT NULL,
        payload TEXT NOT NULL,event_time REAL NOT NULL,available_at REAL NOT NULL,
        observed_at REAL NOT NULL,last_seen REAL NOT NULL,PRIMARY KEY(kind,event_key))""",
    "CREATE INDEX IF NOT EXISTS signals_by_kind_time ON signals(kind,event_time)",
    """CREATE TABLE IF NOT EXISTS signal_sources(
        kind TEXT,source TEXT,symbol TEXT,attempted_at REAL NOT NULL DEFAULT 0,
        succeeded_at REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
        retry_at REAL NOT NULL DEFAULT 0,PRIMARY KEY(kind,source,symbol))""",
    """CREATE TABLE IF NOT EXISTS signal_notifications(
        kind TEXT,event_key TEXT,created_at REAL NOT NULL,PRIMARY KEY(kind,event_key))""",
    """CREATE TABLE IF NOT EXISTS signal_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,symbols TEXT NOT NULL,
        created_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending')""",
    """CREATE TABLE IF NOT EXISTS signal_http_calls(
        provider TEXT,requested_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS signal_http_cache(
        provider TEXT,cache_key TEXT,payload TEXT NOT NULL,fetched_at REAL NOT NULL,
        PRIMARY KEY(provider,cache_key))""",
)


class WorkStopped(ValueError):
    """Cooperative pause/shutdown: never reinterpret this as a provider failure."""


class SignalStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _row(row):
        if not row:
            return None
        keys = (
            "kind",
            "key",
            "symbol",
            "source",
            "payload",
            "event_time",
            "available_at",
            "observed_at",
            "last_seen",
        )
        result = dict(zip(keys, row))
        result["payload"] = json.loads(result["payload"])
        return result

    def get(self, kind, key):
        with self.store.connection() as db:
            return self._row(
                db.execute(
                    "SELECT * FROM signals WHERE kind=? AND event_key=?", (kind, key)
                ).fetchone()
            )

    def put(self, kind, key, symbol, source, payload, *, now, event_time=0, available_at=0):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 32768:
            raise ValueError("Structured signal exceeds its storage limit.")
        if not all(math.isfinite(float(v)) for v in (now, event_time, available_at)):
            raise ValueError("Invalid signal timestamp.")
        available_at = available_at or now
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._row(
                db.execute(
                    "SELECT * FROM signals WHERE kind=? AND event_key=?", (kind, key)
                ).fetchone()
            )
            db.execute(
                "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,event_key) DO UPDATE SET "
                "symbol=excluded.symbol,source=excluded.source,payload=excluded.payload,event_time=excluded.event_time,"
                "available_at=MIN(available_at,excluded.available_at),last_seen=excluded.last_seen",
                (kind, key, symbol, source, encoded, event_time, available_at, now, now),
            )
            return old

    def delete(self, kind, key):
        with self.store.connection() as db:
            db.execute("DELETE FROM signals WHERE kind=? AND event_key=?", (kind, key))

    def rows(self, kind, symbols=None, *, limit=100, since=0):
        if symbols is not None and not symbols:
            return []
        arguments = [kind, since]
        where = "kind=? AND event_time>=?"
        if symbols is not None:
            where += " AND symbol IN (" + ",".join("?" for _ in symbols) + ")"
            arguments.extend(symbols)
        arguments.append(max(1, min(int(limit), 10000)))
        with self.store.connection() as db:
            return [
                self._row(row)
                for row in db.execute(
                    "SELECT * FROM signals WHERE "
                    + where
                    + " ORDER BY event_time DESC,last_seen DESC LIMIT ?",
                    arguments,
                )
            ]

    def due(self, kind, source, symbol, interval, now):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT attempted_at,succeeded_at,error,retry_at FROM signal_sources WHERE kind=? AND source=? AND symbol=?",
                (kind, source, symbol),
            ).fetchone()
        if not row:
            return True
        if row[3] > now:
            return False
        return now - (row[0] if row[2] else max(row[0], row[1])) >= (
            min(interval, 3600) if row[2] else interval
        )

    def attempt(self, kind, source, symbol, now):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO signal_sources(kind,source,symbol,attempted_at) VALUES (?,?,?,?) ON CONFLICT(kind,source,symbol) DO UPDATE SET attempted_at=excluded.attempted_at",
                (kind, source, symbol, now),
            )

    def ok(self, kind, source, symbol, now):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO signal_sources VALUES (?,?,?,?,?,'',0) ON CONFLICT(kind,source,symbol) DO UPDATE SET attempted_at=excluded.attempted_at,succeeded_at=excluded.succeeded_at,error='',retry_at=0",
                (kind, source, symbol, now, now),
            )

    def fail(self, kind, source, symbol, error, now, retry=3600):
        # Caller supplies a fixed sanitized description, never a provider response.
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO signal_sources VALUES (?,?,?,?,0,?,?) ON CONFLICT(kind,source,symbol) DO UPDATE SET attempted_at=excluded.attempted_at,error=excluded.error,retry_at=excluded.retry_at",
                (kind, source, symbol, now, str(error)[:240], now + retry),
            )

    def status(self, kind):
        with self.store.connection() as db:
            return [
                dict(
                    zip(
                        ("source", "symbol", "attempted_at", "succeeded_at", "error", "retry_at"),
                        row,
                    )
                )
                for row in db.execute(
                    "SELECT source,symbol,attempted_at,succeeded_at,error,retry_at FROM signal_sources WHERE kind=? ORDER BY symbol,source",
                    (kind,),
                )
            ]

    def notified(self, kind, key):
        with self.store.connection() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM signal_notifications WHERE kind=? AND event_key=?", (kind, key)
                ).fetchone()
            )

    def notify_once(self, owner_id, kind, key, text, *, now, setting, validator=None):
        from trabe.outbox import Outbox

        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = dict(db.execute("SELECT key,value FROM settings"))
            state = dict(
                db.execute(
                    "SELECT key,value FROM state WHERE key IN ('pause_until','storage_paused')"
                )
            )
            if (
                settings.get(setting) != "on"
                or float(state.get("pause_until", "0")) > now
                or state.get("storage_paused")
            ):
                return False
            if validator is not None and not validator(db):
                return False
            if not db.execute(
                "INSERT OR IGNORE INTO signal_notifications VALUES (?,?,?)", (kind, key, now)
            ).rowcount:
                return False
            Outbox(self.store).send(owner_id, text, kind=kind, db=db)
            return True


class SignalHTTP:
    """Shares price-provider limits without coupling signals to /monitor on."""

    def __init__(self, config, store, *, stop=None, automatic=False, feature=None, opener=urlopen):
        self.config, self.store, self.stop = config, store, stop
        self.automatic, self.feature, self.opener = automatic, feature, opener

    def guard(self):
        if self.stop is not None and self.stop.is_set():
            raise WorkStopped("Background work stopped.")
        if self.automatic:
            settings = self.store.settings()
            if self.store.automation_paused() or self.store.state("storage_paused"):
                raise WorkStopped("Signal automation paused.")
            if self.feature and settings.get(self.feature + "_enabled") != "on":
                raise WorkStopped("Signal automation disabled.")

    def _read(self, url, *, headers=None, maximum=2_000_000):
        self.guard()
        request = Request(
            url, headers={"User-Agent": "trabe/0.7 (financial-watchlist)", **(headers or {})}
        )
        try:
            with self.opener(request, timeout=12) as response:
                raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise ApiError("Signal response exceeds its size limit.")
            self.received_at = time.time()
            return raw
        except HTTPError as error:
            code = error.code
            retry = error.headers.get("Retry-After", "60")
            error.close()
            raise ApiError(
                f"Signal provider HTTP {code}.",
                code,
                min(3600, int(retry)) if str(retry).isdigit() else 60,
            ) from None
        except (URLError, OSError, TimeoutError):
            raise ApiError("Signal provider network request failed.") from None

    @staticmethod
    def _json(raw):
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ApiError("Signal provider returned invalid JSON.") from None
        if not isinstance(result, (dict, list)) or (
            isinstance(result, dict) and result.get("error")
        ):
            raise ApiError("Signal provider returned an unavailable or invalid response.")
        return result

    def finnhub(self, endpoint, **params):
        if not self.config.finnhub_key:
            raise ApiError("Finnhub key is not configured.", 401)
        descriptor = os.open(str(self.store.path) + ".finnhub.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.guard()
            if float(self.store.state("finnhub_retry_after", "0")) > time.time():
                raise ApiError("Finnhub cooldown is active.", 429, 60)
            self.store.reserve_provider(
                "finnhub", int(self.store.settings()["finnhub_daily_budget"])
            )
            self._pace("finnhub_request_finished", 1.4)
            try:
                return self._json(
                    self._read(
                        "https://finnhub.io/api/v1/" + endpoint + "?" + urlencode(params),
                        headers={"X-Finnhub-Token": self.config.finnhub_key},
                    )
                )
            except ApiError as error:
                if error.code == 429:
                    self.store.set_state(
                        "finnhub_retry_after", str(time.time() + max(60, error.retry_after))
                    )
                raise
            finally:
                self.store.set_state("finnhub_request_finished", str(time.time()))
        finally:
            os.close(descriptor)

    def _pace(self, state, seconds):
        delay = max(0, min(seconds, float(self.store.state(state, "0")) + seconds - time.time()))
        if delay:
            if self.stop is not None:
                if self.stop.wait(delay):
                    raise WorkStopped("Background work stopped.")
            else:
                time.sleep(delay)
        self.guard()

    def alpha_csv(self, function, **params):
        if not self.config.api_key:
            raise ApiError("Alpha Vantage key is not configured.", 401)
        key = function + "?" + urlencode(sorted(params.items()))
        descriptor = os.open(str(self.store.path) + ".market.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.guard()
            with self.store.connection() as db:
                cached = db.execute(
                    "SELECT payload,fetched_at FROM signal_http_cache WHERE provider='alphavantage' AND cache_key=?",
                    (key,),
                ).fetchone()
            if cached and 0 <= time.time() - cached[1] < 86400:
                return cached[0]
            if float(self.store.state("market_retry_after", "0")) > time.time():
                raise ApiError("Alpha Vantage cooldown is active.", 429, 60)
            self.store.reserve_request(int(self.store.settings()["api_daily_budget"]))
            self._pace("market_request_finished", 1.1)
            try:
                raw = self._read(
                    "https://www.alphavantage.co/query?"
                    + urlencode({"function": function, **params, "apikey": self.config.api_key}),
                    maximum=8_000_000,
                )
                value = raw.decode("utf-8-sig")
                if value.lstrip().startswith("{"):
                    data = self._json(value)
                    notice = (
                        str(
                            data.get("Note")
                            or data.get("Information")
                            or data.get("Error Message")
                            or ""
                        ).lower()
                        if isinstance(data, dict)
                        else ""
                    )
                    limited = any(
                        word in notice
                        for word in ("limit", "frequency", "requests per", "spreading out")
                    )
                    raise ApiError(
                        "Alpha Vantage calendar request allowance reached."
                        if limited
                        else "Alpha Vantage denied calendar access.",
                        429 if limited else 403,
                        60,
                    )
                if function == "EARNINGS_CALENDAR" and not {"symbol", "reportDate"}.issubset(
                    {v.strip().strip('"') for v in value.splitlines()[0].split(",")}
                    if value.splitlines()
                    else set()
                ):
                    raise ApiError("Alpha Vantage returned an invalid calendar CSV.")
            except ApiError as error:
                if error.code == 429:
                    self.store.set_state(
                        "market_retry_after", str(time.time() + max(60, error.retry_after))
                    )
                raise
            except UnicodeError:
                raise ApiError("Alpha Vantage returned invalid calendar CSV.") from None
            finally:
                self.store.set_state("market_request_finished", str(time.time()))
            with self.store.connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO signal_http_cache VALUES ('alphavantage',?,?,?)",
                    (key, value, time.time()),
                )
            return value
        finally:
            os.close(descriptor)

    def _public(self, url, provider):
        parsed = urlsplit(url)
        allowed = {
            "tracefour": {"tracefour.com"},
            "yahoo": {"feeds.finance.yahoo.com", "finance.yahoo.com"},
            "sec": {"www.sec.gov", "data.sec.gov"},
        }
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed.get(provider, set())
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Unapproved public feed URL.")
        descriptor = os.open(
            str(self.store.path) + f".{provider}.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.guard()
            now = time.time()
            if float(self.store.state(provider + "_retry_after", "0")) > now:
                raise ApiError("Public feed cooldown is active.", 429, 60)
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM signal_http_calls WHERE requested_at<?", (now - 3600,))
                count = db.execute(
                    "SELECT COUNT(*) FROM signal_http_calls WHERE provider=?", (provider,)
                ).fetchone()[0]
                if count >= (40 if provider == "tracefour" else 120):
                    raise ApiError("Public feed hourly allowance reached.", 429, 3600)
                db.execute("INSERT INTO signal_http_calls VALUES (?,?)", (provider, now))
            try:
                return self._read(url, maximum=2_000_000)
            except ApiError as error:
                if error.code == 429:
                    self.store.set_state(
                        provider + "_retry_after", str(now + max(60, error.retry_after))
                    )
                raise
        finally:
            os.close(descriptor)

    def json(self, url, provider="tracefour"):
        return self._json(self._public(url, provider))

    def xml(self, url, provider="yahoo"):
        return self._public(url, provider)
