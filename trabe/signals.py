"""Shared, bounded event storage and budgeted outbound fundamentals requests."""

import fcntl
import json
import math
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from trabe import __version__
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


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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

    def __init__(
        self,
        config,
        store,
        *,
        stop=None,
        automatic=False,
        feature=None,
        opener=urlopen,
        deadline=None,
        guard_settings=None,
    ):
        self.config, self.store, self.stop = config, store, stop
        self.automatic, self.feature, self.opener = automatic, feature, opener
        self.deadline, self.guard_settings = deadline, guard_settings or {}

    def guard(self):
        if self.stop is not None and self.stop.is_set():
            raise WorkStopped("Background work stopped.")
        if self.automatic:
            settings = self.store.settings()
            if self.deadline is not None and time.time() >= self.deadline:
                raise WorkStopped("Automatic refresh deadline passed.")
            if any(settings.get(k) != v for k, v in self.guard_settings.items()):
                raise WorkStopped("Automatic refresh settings changed.")
            if self.store.automation_paused() or self.store.state("storage_paused"):
                raise WorkStopped("Signal automation paused.")
            if self.feature and settings.get(self.feature + "_enabled") != "on":
                raise WorkStopped("Signal automation disabled.")

    def _read(self, url, *, headers=None, maximum=2_000_000, opener=None):
        self.guard()
        request = Request(
            url,
            headers={"User-Agent": f"trabe/{__version__} (financial-watchlist)", **(headers or {})},
        )
        try:
            with (opener or self.opener)(request, timeout=12) as response:
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

    def news_api(self, provider, **params):
        """One bounded page; no authenticated redirects or arbitrary next_url fetches."""
        if provider not in {"massive", "marketaux"}:
            raise ValueError("Unknown news API.")
        key = getattr(self.config, provider + "_key", "")
        if not key:
            raise ApiError("News API key is not configured.", 401)
        descriptor = os.open(
            str(self.store.path) + f".{provider}.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.guard()
            now = time.time()
            if float(self.store.state(provider + "_retry_after", "0")) > now:
                raise ApiError("News API cooldown is active.", 429, 60)
            state_key = None
            if provider == "marketaux":
                state_key = "marketaux_checks_" + params.get("symbols", "")
                count = json.loads(self.store.state(state_key, "{}"))
                day = self.store.today()
                used = count.get("count", 0) if count.get("day") == day else 0
                if used >= 6:
                    raise ApiError("News request budget deferred.", 429, 3600)
            budget = int(self.store.settings()[provider + "_daily_budget"])
            if self.store.provider_usage(provider) >= budget:
                raise ApiError("News request budget deferred.", 429, 3600)
            self._pace(provider + "_request_finished", 15.1 if provider == "massive" else 2.1)
            if self.automatic and provider not in self.store.settings()["news_sources"].split(","):
                raise WorkStopped("News source disabled during automatic work.")
            try:
                self.store.reserve_provider(
                    provider,
                    budget,
                    per_minute=4 if provider == "massive" else 30,
                )
            except ValueError:
                raise ApiError("News request budget deferred.", 429, 60) from None
            if state_key is not None:
                self.store.set_state(state_key, json.dumps({"day": day, "count": used + 1}))
            if provider == "massive":
                url = "https://api.massive.com/v2/reference/news?" + urlencode(params)
                headers = {"Authorization": "Bearer " + key}
            else:
                url = "https://api.marketaux.com/v1/news/all?" + urlencode(
                    params | {"api_token": key}
                )
                headers = {}
            try:
                opener = build_opener(NoRedirect()).open if self.opener is urlopen else self.opener
                result = self._json(self._read(url, headers=headers, opener=opener))
                if not isinstance(result, dict) or result.get("status") in {
                    "ERROR",
                    "NOT_AUTHORIZED",
                }:
                    raise ApiError("News API response is unavailable.")
                return result
            except ApiError as error:
                if error.code in {401, 403, 429}:
                    self.store.set_state(
                        provider + "_retry_after",
                        str(
                            time.time()
                            + max(3600 if error.code in {401, 403} else 60, error.retry_after)
                        ),
                    )
                raise
            finally:
                self.store.set_state(provider + "_request_finished", str(time.time()))
        finally:
            os.close(descriptor)

    def alpha_csv(self, function, **params):
        return self._alpha(function, params, json_response=False, ttl=86400)

    def alpha_json(self, function, **params):
        if function not in {"EARNINGS", "INCOME_STATEMENT"}:
            raise ValueError("Unsupported Alpha Vantage fundamentals endpoint.")
        return self._json(self._alpha(function, params, json_response=True, ttl=3600))

    def _alpha(self, function, params, *, json_response, ttl):
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
            if cached and 0 <= time.time() - cached[1] < ttl:
                self.alpha_checked_at = cached[1]
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
                    if notice:
                        raise ApiError(
                            "Alpha Vantage request allowance reached."
                            if limited
                            else "Alpha Vantage denied endpoint access.",
                            429 if limited else 403,
                            60,
                        )
                    if not json_response:
                        raise ApiError("Alpha Vantage denied calendar access.", 403)
                    rows_key = "quarterlyEarnings" if function == "EARNINGS" else "quarterlyReports"
                    if not isinstance(data, dict) or not isinstance(data.get(rows_key), list):
                        raise ApiError("Alpha Vantage returned invalid fundamentals JSON.")
                    fields = (
                        ("fiscalDateEnding", "reportedDate", "reportedEPS", "estimatedEPS")
                        if function == "EARNINGS"
                        else ("fiscalDateEnding", "reportedCurrency", "totalRevenue")
                    )
                    data = {
                        "symbol": data.get("symbol"),
                        rows_key: [
                            {field: row.get(field) for field in fields}
                            for row in data[rows_key][:8]
                            if isinstance(row, dict)
                        ],
                    }
                    value = json.dumps(data, allow_nan=False)
                elif json_response:
                    raise ApiError("Alpha Vantage returned invalid fundamentals JSON.")
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
                self.alpha_checked_at = time.time()
                db.execute(
                    "INSERT OR REPLACE INTO signal_http_cache VALUES ('alphavantage',?,?,?)",
                    (key, value, self.alpha_checked_at),
                )
            return value
        finally:
            os.close(descriptor)

    def _public(self, url, provider):
        parsed = urlsplit(url)
        allowed = {
            "tracefour": {"tracefour.com"},
            "yahoo": {"feeds.finance.yahoo.com", "finance.yahoo.com"},
            "yahoo-chart": {"query1.finance.yahoo.com"},
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
        market_chart = provider == "yahoo-chart"
        shared_provider = "yahoo" if market_chart else provider
        descriptor = os.open(
            str(self.store.path) + f".{shared_provider}.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.guard()
            now = time.time()
            if (
                max(
                    float(self.store.state(shared_provider + "_retry_after", "0")),
                    float(self.store.state(provider + "_retry_after", "0")),
                )
                > now
            ):
                raise ApiError("Public feed cooldown is active.", 429, 60)
            if market_chart:
                self._pace("yahoo_chart_request_finished", 1.1)
                self.guard()
                now = time.time()
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM signal_http_calls WHERE requested_at<?", (now - 3600,))
                count = db.execute(
                    "SELECT COUNT(*) FROM signal_http_calls WHERE provider IN (?,?)",
                    (
                        shared_provider,
                        "yahoo-chart" if shared_provider == "yahoo" else shared_provider,
                    ),
                ).fetchone()[0]
                if count >= (40 if provider == "tracefour" else 120):
                    raise ApiError("Public feed hourly allowance reached.", 429, 3600)
                if (
                    market_chart
                    and db.execute(
                        "SELECT COUNT(*) FROM signal_http_calls WHERE provider='yahoo-chart'"
                    ).fetchone()[0]
                    >= 60
                ):
                    raise ApiError(
                        "Market chart hourly allowance reached; news headroom reserved.", 429, 3600
                    )
                db.execute("INSERT INTO signal_http_calls VALUES (?,?)", (provider, now))
            try:
                opener = (
                    build_opener(NoRedirect()).open
                    if market_chart and self.opener is urlopen
                    else self.opener
                )
                return self._read(url, maximum=2_000_000, opener=opener)
            except ApiError as error:
                if error.code == 429:
                    self.store.set_state(
                        shared_provider + "_retry_after", str(now + max(60, error.retry_after))
                    )
                elif market_chart and error.code in (401, 403):
                    self.store.set_state("yahoo-chart_retry_after", str(now + 3600))
                raise
            finally:
                if market_chart:
                    self.store.set_state("yahoo_chart_request_finished", str(time.time()))
        finally:
            os.close(descriptor)

    def json(self, url, provider="tracefour"):
        return self._json(self._public(url, provider))

    def xml(self, url, provider="yahoo"):
        return self._public(url, provider)
