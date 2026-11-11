"""Durable settings, polling offset, watchlist, quote cache, and API budget."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from trabe.preferences import DEFAULTS, validate_setting
from trabe.storage import SCHEMA, MarketStorage

SCHEMA_VERSION = 9


class Store(MarketStorage):
    def __init__(self, path: Path):
        from trabe.advisor import ADVISOR_SCHEMA
        from trabe.news_store import NEWS_SCHEMA, migrate_legacy
        from trabe.outlook import OUTLOOK_SCHEMA
        from trabe.signals import SIGNAL_SCHEMA
        from trabe.snapshots import SNAPSHOT_SCHEMA

        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(
                    "Database schema is newer than this application; use the matching release."
                )
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS watchlist (
                    ticker TEXT PRIMARY KEY, tier INTEGER NOT NULL CHECK(tier BETWEEN 1 AND 3)
                );
                CREATE TABLE IF NOT EXISTS quote_cache (
                    ticker TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_usage (day TEXT PRIMARY KEY, requests INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS scan_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    finished_at REAL,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','interrupted')),
                    summary TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_scan ON scan_jobs(status) WHERE status='running';
                CREATE TABLE IF NOT EXISTS daily_runs (day TEXT PRIMARY KEY, job_id INTEGER);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    markup TEXT,
                    created_at REAL NOT NULL
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            for statement in (*SCHEMA, *SIGNAL_SCHEMA, *NEWS_SCHEMA):
                db.execute(statement)
            news_columns = {row[1] for row in db.execute("PRAGMA table_info(news_revisions)")}
            for column in ("correction_of", "url"):
                if column not in news_columns:
                    db.execute(
                        f"ALTER TABLE news_revisions ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            db.execute(
                "UPDATE news_revisions SET url=(SELECT url FROM news_articles WHERE id=article_id) WHERE url=''"
            )
            if version < 6:
                migrate_legacy(db)
                db.execute("PRAGMA user_version=6")
            for statement in (*SNAPSHOT_SCHEMA, *OUTLOOK_SCHEMA, *ADVISOR_SCHEMA):
                db.execute(statement)
            trade_columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
            for column in ("fx_from", "fx_to"):
                if column not in trade_columns:
                    db.execute(f"ALTER TABLE trades ADD COLUMN {column} TEXT")
            db.execute("""UPDATE trades SET fx_from=currency,
                fx_to=CASE currency WHEN 'EUR' THEN 'USD' ELSE 'EUR' END
                WHERE fx_rate IS NOT NULL AND (fx_from IS NULL OR fx_to IS NULL)""")
            if "kind" not in {row[1] for row in db.execute("PRAGMA table_info(outbox)")}:
                db.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'reply'")
            db.executemany("INSERT OR IGNORE INTO settings VALUES (?, ?)", DEFAULTS.items())
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def connection(self):
        # sqlite3's own context manager commits but does not close connections.
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self, db=None) -> dict[str, str]:
        if db is None:
            with self.connection() as connection:
                return self.settings(connection)
        return dict(db.execute("SELECT key, value FROM settings"))

    def set_setting(self, key: str, value: str):
        value = validate_setting(key, value)
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    def state(self, key: str, default: str = "") -> str:
        with self.connection() as db:
            row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def set_state(self, key: str, value: str):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))

    def watchlist(self, db=None) -> list[tuple[str, int]]:
        if db is None:
            with self.connection() as connection:
                return self.watchlist(connection)
        return db.execute("SELECT ticker, tier FROM watchlist ORDER BY tier, ticker").fetchall()

    def add(self, ticker: str, tier: int):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] >= 100:
                if not db.execute("SELECT 1 FROM watchlist WHERE ticker = ?", (ticker,)).fetchone():
                    raise ValueError("Watchlist limit is 100 tickers.")
            db.execute("INSERT OR REPLACE INTO watchlist VALUES (?, ?)", (ticker, tier))

    def remove(self, ticker: str) -> bool:
        with self.connection() as db:
            return db.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,)).rowcount > 0

    @staticmethod
    def today() -> str:
        return datetime.now(UTC).date().isoformat()

    def usage(self) -> int:
        with self.connection() as db:
            row = db.execute(
                "SELECT requests FROM api_usage WHERE day = ?", (self.today(),)
            ).fetchone()
            return row[0] if row else 0

    def reserve_request(self, budget: int):
        """Count attempts before network I/O, including provider errors/timeouts."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            day = self.today()
            db.execute("INSERT OR IGNORE INTO api_usage VALUES (?, 0)", (day,))
            changed = db.execute(
                "UPDATE api_usage SET requests=requests+1 WHERE day=? AND requests < ?",
                (day, budget),
            ).rowcount
            if not changed:
                raise ValueError(
                    "Local API budget reached for today (UTC). Cached quotes still work."
                )

    def cached_quote(self, ticker: str, ttl_minutes: int) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload, fetched_at FROM quote_cache WHERE ticker=?", (ticker,)
            ).fetchone()
            if row and 0 <= time.time() - row[1] < ttl_minutes * 60:
                return json.loads(row[0])
            return None

    def save_quote(self, ticker: str, quote: dict):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO quote_cache VALUES (?, ?, ?)",
                (ticker, json.dumps(quote), time.time()),
            )

    def all_cached_quotes(self) -> dict[str, tuple[dict, float]]:
        with self.connection() as db:
            return {
                ticker: (json.loads(payload), fetched_at)
                for ticker, payload, fetched_at in db.execute(
                    "SELECT ticker,payload,fetched_at FROM quote_cache"
                )
            }

    def start_scan(self, day: str | None = None) -> int | None:
        """Claim one job and optionally one scheduled day in a single transaction."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM scan_jobs WHERE status='running'").fetchone():
                return None
            if day and db.execute("SELECT 1 FROM daily_runs WHERE day=?", (day,)).fetchone():
                return None
            cursor = db.execute(
                "INSERT INTO scan_jobs(created_at,status) VALUES (?,'running')", (time.time(),)
            )
            job_id = cursor.lastrowid
            if day:
                db.execute("INSERT INTO daily_runs VALUES (?,?)", (day, job_id))
            return job_id

    def finish_scan(self, job_id: int, summary: str, status: str = "completed"):
        with self.connection() as db:
            db.execute(
                "UPDATE scan_jobs SET finished_at=?,status=?,summary=? WHERE id=?",
                (time.time(), status, summary, job_id),
            )

    def latest_scan(self) -> tuple | None:
        with self.connection() as db:
            return db.execute(
                "SELECT id,status,summary FROM scan_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def interrupt_scans(self):
        with self.connection() as db:
            db.execute(
                "UPDATE scan_jobs SET status='interrupted',finished_at=? WHERE status='running'",
                (time.time(),),
            )

    def backup(self, destination: Path):
        """SQLite's online backup API produces a consistent snapshot, including WAL."""
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        try:
            with self.connection() as source:
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                finally:
                    target.close()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
