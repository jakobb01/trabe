"""Version-three market storage. Methods share Store's short-lived connections."""

import json
import time

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
        side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
        currency TEXT NOT NULL, trade_date TEXT NOT NULL, fee TEXT NOT NULL,
        fx_rate TEXT, fx_date TEXT, fx_provider TEXT, fx_retrieved TEXT, fx_from TEXT, fx_to TEXT,
        created_at TEXT NOT NULL, voided_at TEXT, update_id INTEGER UNIQUE)""",
    """CREATE TABLE IF NOT EXISTS fx_cache (
        requested_date TEXT PRIMARY KEY, usd_per_eur TEXT NOT NULL,
        effective_date TEXT NOT NULL, provider TEXT NOT NULL, retrieved_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fx_attempts (
        requested_date TEXT PRIMARY KEY, attempted_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        day TEXT PRIMARY KEY, currency TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS provider_usage(provider TEXT,day TEXT,requests INTEGER NOT NULL,PRIMARY KEY(provider,day))",
    "CREATE TABLE IF NOT EXISTS provider_calls(provider TEXT,requested_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS calls_by_provider_time ON provider_calls(provider,requested_at)",
    "CREATE TABLE IF NOT EXISTS live_quotes(provider TEXT,ticker TEXT,payload TEXT NOT NULL,received_at REAL NOT NULL,PRIMARY KEY(provider,ticker))",
    "CREATE TABLE IF NOT EXISTS alert_preferences(ticker TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 1,threshold REAL)",
    "CREATE TABLE IF NOT EXISTS instruments(id INTEGER PRIMARY KEY,symbol TEXT NOT NULL UNIQUE,currency TEXT NOT NULL DEFAULT 'USD')",
    "CREATE TABLE IF NOT EXISTS feeds(id INTEGER PRIMARY KEY,provider TEXT NOT NULL,feed TEXT NOT NULL,UNIQUE(provider,feed))",
    """CREATE TABLE IF NOT EXISTS market_sessions(
        session_date TEXT PRIMARY KEY,opens REAL NOT NULL,closes REAL NOT NULL,
        finalized INTEGER NOT NULL DEFAULT 0,quality_flags INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS session_symbols(
        session_date TEXT,instrument_id INTEGER,feed_id INTEGER,
        started_at REAL NOT NULL,PRIMARY KEY(session_date,instrument_id,feed_id)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS baselines(
        ticker TEXT,provider TEXT,session_date TEXT,previous_date TEXT NOT NULL,
        previous_close REAL NOT NULL,open_price REAL NOT NULL,source_time REAL NOT NULL,
        accepted INTEGER NOT NULL,reason TEXT NOT NULL,
        PRIMARY KEY(ticker,provider,session_date)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS minute_observations(
        instrument_id INTEGER NOT NULL,feed_id INTEGER NOT NULL,session_date TEXT NOT NULL,
        minute_start_ms INTEGER NOT NULL,observed_open REAL NOT NULL,observed_high REAL NOT NULL,
        observed_low REAL NOT NULL,observed_last REAL NOT NULL,first_source_ms INTEGER NOT NULL,
        last_source_ms INTEGER NOT NULL,tick_count INTEGER NOT NULL,quality_flags INTEGER NOT NULL,
        PRIMARY KEY(session_date,instrument_id,feed_id,minute_start_ms)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS daily_summaries(
        instrument_id INTEGER NOT NULL,feed_id INTEGER NOT NULL,session_date TEXT NOT NULL,
        observed_open REAL,observed_high REAL,observed_low REAL,observed_last REAL,
        previous_close REAL,previous_date TEXT,baseline_source_time REAL,
        first_source_ms INTEGER,last_source_ms INTEGER,observed_minutes INTEGER NOT NULL,
        expected_minutes INTEGER NOT NULL,max_gap_seconds INTEGER NOT NULL,
        alert_count INTEGER NOT NULL,quality_flags INTEGER NOT NULL,
        PRIMARY KEY(instrument_id,feed_id,session_date)) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS summaries_by_date ON daily_summaries(session_date)",
    """CREATE TABLE IF NOT EXISTS alert_events(
        id INTEGER PRIMARY KEY,ticker TEXT NOT NULL,rule TEXT NOT NULL,session_date TEXT NOT NULL,
        price REAL NOT NULL,previous_close REAL NOT NULL,change_percent REAL NOT NULL,
        threshold REAL NOT NULL,source_ms INTEGER NOT NULL,provider TEXT NOT NULL,
        feed TEXT NOT NULL,created_at REAL NOT NULL,
        UNIQUE(ticker,rule,session_date))""",
)


class MarketStorage:
    def select_provider(self, provider: str):
        """Switch explicitly and invalidate only the current session's alert inputs."""
        from datetime import datetime

        from trabe.calendar import NY

        if provider not in ("finnhub", "alphavantage"):
            raise ValueError("Unknown market provider.")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT value FROM settings WHERE key='market_provider'"
            ).fetchone()[0]
            if current != provider:
                db.execute("UPDATE settings SET value=? WHERE key='market_provider'", (provider,))
                db.execute("UPDATE settings SET value='off' WHERE key='monitoring'")
                db.execute(
                    "UPDATE baselines SET accepted=0,reason='Provider changed; revalidation required.' WHERE session_date=?",
                    (datetime.now(NY).date().isoformat(),),
                )
                db.execute("DELETE FROM outbox WHERE kind='alert'")

    def provider_usage(self, provider: str) -> int:
        with self.connection() as db:
            row = db.execute(
                "SELECT requests FROM provider_usage WHERE provider=? AND day=?",
                (provider, self.today()),
            ).fetchone()
            return row[0] if row else 0

    def reserve_provider(self, provider: str, budget: int, per_minute: int = 45):
        now = time.time()
        name = {"finnhub": "Finnhub", "massive": "Massive", "marketaux": "Marketaux"}.get(
            provider, "Provider"
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM provider_calls WHERE requested_at < ?", (now - 60,))
            count = db.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE provider=?", (provider,)
            ).fetchone()[0]
            if count >= per_minute:
                raise ValueError(f"{name} minute allowance reserved; retry in a minute.")
            db.execute(
                "INSERT OR IGNORE INTO provider_usage VALUES (?,?,0)", (provider, self.today())
            )
            if not db.execute(
                "UPDATE provider_usage SET requests=requests+1 WHERE provider=? AND day=? AND requests<?",
                (provider, self.today(), budget),
            ).rowcount:
                raise ValueError(
                    f"{name} daily REST budget reached; cached data remains available."
                )
            db.execute("INSERT INTO provider_calls VALUES (?,?)", (provider, now))

    def cached_live(self, provider: str, ticker: str, db=None):
        if db is None:
            with self.connection() as connection:
                return self.cached_live(provider, ticker, connection)
        row = db.execute(
            "SELECT payload FROM live_quotes WHERE provider=? AND ticker=?", (provider, ticker)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_live(self, quote: dict, db=None):
        if db is None:
            with self.connection() as connection:
                self.save_live(quote, connection)
            return
        db.execute(
            "INSERT INTO live_quotes VALUES (?,?,?,?) ON CONFLICT(provider,ticker) "
            "DO UPDATE SET payload=excluded.payload,received_at=excluded.received_at",
            (quote["provider"], quote["ticker"], json.dumps(quote), quote["received_at"]),
        )

    def automation_paused(self, now: float | None = None) -> bool:
        return float(self.state("pause_until", "0")) > (time.time() if now is None else now)

    def pause_automation(self, until: float):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO state VALUES ('pause_until',?)", (str(until),))
            if not until:
                db.execute(
                    "INSERT OR REPLACE INTO state VALUES ('insights_resumed_at',?)",
                    (str(time.time()),),
                )
            db.execute("DELETE FROM outbox WHERE kind!='reply'")
            if until:
                db.execute(
                    "UPDATE signal_jobs SET status='cancelled' WHERE status IN ('pending','running') AND json_extract(symbols,'$.origin')='automatic'"
                )

    def alert_rule(self, ticker: str) -> tuple[bool, float]:
        with self.connection() as db:
            row = db.execute(
                "SELECT enabled,threshold FROM alert_preferences WHERE ticker=?", (ticker,)
            ).fetchone()
            default = float(self.settings()["dip_threshold_percent"])
            return (
                (bool(row[0]), row[1] if row[1] is not None else default)
                if row
                else (True, default)
            )

    def set_alert_rule(self, ticker: str, value: str):
        from trabe.preferences import validate_setting

        if ticker not in dict(self.watchlist()):
            raise ValueError("Add this symbol to your watchlist before configuring its alert.")
        if value in ("off", "default"):
            enabled, threshold = int(value != "off"), None
        else:
            enabled, threshold = 1, float(validate_setting("dip_threshold_percent", value))
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO alert_preferences VALUES (?,?,?)",
                (ticker, enabled, threshold),
            )

    def baseline(self, ticker: str, session_date: str):
        with self.connection() as db:
            db.row_factory = __import__("sqlite3").Row
            row = db.execute(
                "SELECT * FROM baselines WHERE ticker=? AND provider='finnhub' AND session_date=?",
                (ticker, session_date),
            ).fetchone()
            return dict(row) if row else None

    def save_baseline(self, ticker: str, session_date: str, values: dict):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO baselines VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ticker,
                    "finnhub",
                    session_date,
                    values["previous_date"],
                    values["previous_close"],
                    values["open_price"],
                    values["source_time"],
                    int(values["accepted"]),
                    values["reason"],
                ),
            )

    @staticmethod
    def instrument(db, ticker: str) -> int:
        db.execute("INSERT OR IGNORE INTO instruments(symbol) VALUES (?)", (ticker,))
        return db.execute("SELECT id FROM instruments WHERE symbol=?", (ticker,)).fetchone()[0]

    @staticmethod
    def feed(db, provider: str = "finnhub", feed: str = "us-trades") -> int:
        db.execute("INSERT OR IGNORE INTO feeds(provider,feed) VALUES (?,?)", (provider, feed))
        return db.execute(
            "SELECT id FROM feeds WHERE provider=? AND feed=?", (provider, feed)
        ).fetchone()[0]

    def join_session(self, session, ticker: str, now: float) -> tuple[int, int]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO market_sessions(session_date,opens,closes) VALUES (?,?,?)",
                (session.day, session.opens, session.closes),
            )
            if db.execute(
                "SELECT finalized FROM market_sessions WHERE session_date=?", (session.day,)
            ).fetchone()[0]:
                raise ValueError("This exchange session has already been finalized.")
            instrument, feed = self.instrument(db, ticker), self.feed(db)
            db.execute(
                "INSERT OR IGNORE INTO session_symbols VALUES (?,?,?,?)",
                (session.day, instrument, feed, now),
            )
            return instrument, feed

    def save_bucket(self, bucket: dict, db=None):
        if db is None:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self.save_bucket(bucket, connection)
            return
        finalized = db.execute(
            "SELECT finalized FROM market_sessions WHERE session_date=?", (bucket["session_date"],)
        ).fetchone()
        if not finalized or finalized[0]:
            return
        db.execute(
            """INSERT INTO minute_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_date,instrument_id,feed_id,minute_start_ms) DO UPDATE SET
            observed_open=CASE WHEN excluded.first_source_ms<first_source_ms THEN excluded.observed_open ELSE observed_open END,
            observed_high=MAX(observed_high,excluded.observed_high),
            observed_low=MIN(observed_low,excluded.observed_low),
            observed_last=CASE WHEN excluded.last_source_ms>=last_source_ms THEN excluded.observed_last ELSE observed_last END,
            first_source_ms=MIN(first_source_ms,excluded.first_source_ms),
            last_source_ms=MAX(last_source_ms,excluded.last_source_ms),
            tick_count=MAX(tick_count,excluded.tick_count),
            quality_flags=quality_flags|excluded.quality_flags""",
            tuple(
                bucket[name]
                for name in (
                    "instrument_id",
                    "feed_id",
                    "session_date",
                    "minute_start_ms",
                    "observed_open",
                    "observed_high",
                    "observed_low",
                    "observed_last",
                    "first_source_ms",
                    "last_source_ms",
                    "tick_count",
                    "quality_flags",
                )
            ),
        )

    def stored_bucket(self, instrument: int, feed: int, day: str, minute_ms: int):
        with self.connection() as db:
            db.row_factory = __import__("sqlite3").Row
            row = db.execute(
                "SELECT * FROM minute_observations WHERE session_date=? AND instrument_id=? AND feed_id=? AND minute_start_ms=?",
                (day, instrument, feed, minute_ms),
            ).fetchone()
            return dict(row) if row else None

    def mark_session_gap(self, day: str, flag: int = 1):
        with self.connection() as db:
            db.execute(
                "UPDATE market_sessions SET quality_flags=quality_flags|? WHERE session_date=?",
                (flag, day),
            )

    def history(self, ticker: str, count: int = 5) -> list[dict]:
        with self.connection() as db:
            db.row_factory = __import__("sqlite3").Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT d.*,i.symbol,f.provider,f.feed FROM daily_summaries d "
                    "JOIN instruments i ON i.id=d.instrument_id JOIN feeds f ON f.id=d.feed_id "
                    "WHERE i.symbol=? ORDER BY session_date DESC LIMIT ?",
                    (ticker, count),
                )
            ]

    def recent_alerts(self, count: int = 10) -> list[dict]:
        with self.connection() as db:
            db.row_factory = __import__("sqlite3").Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM alert_events ORDER BY id DESC LIMIT ?", (count,)
                )
            ]

    def intraday_quote(self, ticker: str):
        result = self.cached_live("finnhub", ticker)
        if result:
            result["age_seconds"] = max(0, time.time() - result["source_time"])
        return result
